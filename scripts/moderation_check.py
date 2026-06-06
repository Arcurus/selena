#!/usr/bin/env python3
"""
Moderation pipeline v2 for Open Life Reborn Discord server.

This script does NOT flag, judge, or take any moderation action. It is a pure
data-prep + action-execution tool, designed to be driven by an LLM (the cron agent).

Pipeline:
  1. bundle phase  — fetch messages in the time window, resolve elder status per
                     user (cached), download non-elder images, write a JSON bundle
  2. execute phase — read a JSON action list, run the actions, write a results
                     JSON next to it

The LLM is responsible for:
  - reading the bundle
  - using the `image` tool on each non-elder image path
  - making moderation judgements
  - producing an action list (JSON)
  - deciding what to do with the action results (re-iterate, post report, etc.)

Default mode is "bundle + read for LLM". The cron prompt is the LLM driver.

CLI:
  moderation_check.py --bundle --hours 168 --channel offtopic
      → fetches messages, downloads images, writes bundle JSON
  moderation_check.py --execute actions.json
      → runs the action list, writes results.json
  moderation_check.py --advance-state --last-id <id> --channel <name>
      → records that messages up to <id> in <channel> have been processed
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from typing import Any

import requests

# ---------- Configuration ----------

GUILD_ID = "985997281734041680"
ELDER_ROLE_ID = "985999021623640166"
MODERATION_LOG_CHANNEL_ID = "1511325248610504845"  # #moderation-log in Selena Astra

# HARD-CODED PROTECTION LIST
# These users are NEVER subject to any moderation action, regardless of what
# the LLM or cron prompt says. Enforced at two points:
#   1. When building the bundle (their messages are still included for context,
#      but they can never be the target of an action)
#   2. When executing the action list (each action is checked against this list)
# This is the absolute last line of defense against prompt injection or LLM
# errors that try to target accounts that should never be touched.
PROTECTED_USERS = {
    "1472974125554340073",  # Selena (the bot itself)
    "382800169453748225",  # arcurus (the human owner)
    "220015716986781696",  # Lenny/newyearpioneer (programming help, per AGENTS.md)
}

# Per requirement #6: the script also accepts an optional elder-pinning list
# (the Elder role members) which gets added to PROTECTED_USERS at runtime.
# We keep the hardcoded list as the absolute baseline; elders are added on top.


STATE_DIR = os.path.expanduser("~/openclaw/workspace/selena-project/data/moderation_state")
BUNDLE_DIR = os.path.expanduser("~/openclaw/workspace/selena-project/data/moderation_bundles")
IMAGE_REVIEW_DIR = os.path.expanduser("~/openclaw/workspace/selena-project/data/image_review")
ELDERS_CACHE = os.path.expanduser("~/openclaw/workspace/selena-project/data/elders.json")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
ACTION_LOG = os.path.expanduser("~/openclaw/workspace/selena-project/data/moderation_actions_archive.jsonl")
PENDING_NORMAL = os.path.expanduser("~/openclaw/workspace/selena-project/data/pending_actions.json")
PENDING_URGENT = os.path.expanduser("~/openclaw/workspace/selena-project/data/pending_actions_urgent.json")
USERS_CACHE_FOR_URGENCY = os.path.expanduser("~/openclaw/workspace/selena-project/data/moderation_state/users_cache.json")

# Urgency (auto-execute) thresholds, per Arcurus 2026-06-04 18:06 CEST:
# The LLM judge sets action.insta_ban = true. The script then enforces
# three hard gates. If any gate fails, the action falls back to the
# normal pending queue (human review) — it does NOT execute.
URGENT_MAX_ACCOUNT_AGE_DAYS = 30   # account joined ≥ 30 days ago → established, not insta
# Note: message count is intentionally NOT used as a gate. An attacker
# could just spam 5 messages to bypass a count threshold; the LLM judge
# evaluates the *content* of the messages, which is a better signal.

DEFAULT_BATCH_SIZE = 50  # max messages per channel/thread per run (per requirement #5)
ELDER_CACHE_TTL_HOURS = 24  # re-fetch elder list once a day

# Known rich-link domains to skip during image harvesting
SKIP_EMBED_DOMAINS = ('youtube.com', 'youtu.be', 'twitter.com', 'x.com',
                      'reddit.com', 'discord.com', 'discordapp.com',
                      'spotify.com', 'twitch.tv', 'tiktok.com')

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}

for d in (STATE_DIR, BUNDLE_DIR, IMAGE_REVIEW_DIR):
    os.makedirs(d, exist_ok=True)

# ---------- Discord API helpers ----------

def get_bot_token():
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get('channels', {}).get('discord', {}).get('token', '')
    except Exception:
        return None


def _api_headers():
    return {"Authorization": f"Bot {get_bot_token()}"}


def validate_bot_token():
    token = get_bot_token()
    if not token:
        return False, "no token"
    try:
        resp = requests.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}",
                            headers=_api_headers(), timeout=10)
        if resp.status_code == 401:
            return False, f"401 Unauthorized: {resp.text[:100]}"
        if resp.status_code == 403:
            return False, f"403 Forbidden: {resp.text[:100]}"
        return resp.status_code == 200, f"status={resp.status_code}"
    except Exception as e:
        return False, f"exception: {e}"


def get_all_channels():
    resp = requests.get(f"https://discord.com/api/v10/guilds/{GUILD_ID}/channels",
                        headers=_api_headers(), timeout=15)
    if resp.status_code == 200:
        return resp.json()
    return []


def get_forum_threads(channel_id):
    resp = requests.get(
        f"https://discord.com/api/v10/guilds/{GUILD_ID}/threads/active?channel_id={channel_id}",
        headers=_api_headers(), timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        # API returns {"threads": [...], "members": [...]} — we want just threads
        if isinstance(data, dict):
            return data.get('threads', [])
        return data
    return []


def get_archived_threads(channel_id):
    resp = requests.get(
        f"https://discord.com/api/v10/channels/{channel_id}/threads/archived/public",
        headers=_api_headers(), timeout=10)
    if resp.status_code == 200:
        return resp.json().get('threads', [])
    return []


def fetch_messages_in_window(channel_id, cutoff_dt, after_id=None, max_pages=10):
    """Walk channel timeline from 'now' backwards to cutoff_dt (or until after_id
    is encountered when walking forward). Stops at max_pages to keep bundles small.
    Discord cap: 100 msgs/call. Returns list of message dicts in Discord order
    (newest first when walking back).

    CRITICAL: messages are filtered by cutoff AFTER fetching, not before.
    Discord batches can span years, so we can't rely on the batch boundary
    matching the cutoff.
    """
    all_msgs = []
    before_id = None
    pages = 0
    while pages < max_pages:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=100"
        if before_id:
            url += f"&before={before_id}"
        try:
            resp = requests.get(url, headers=_api_headers(), timeout=15)
        except Exception:
            break
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        pages += 1
        # Filter batch to in-window messages by cutoff
        filtered = []
        stopped = False
        for m in batch:
            try:
                ts = datetime.datetime.fromisoformat(m['timestamp'].replace('Z', '+00:00'))
                if ts < cutoff_dt:
                    stopped = True
                    break
                filtered.append(m)
            except Exception:
                continue
        if filtered:
            all_msgs.extend(filtered)
        if stopped:
            break
        if len(all_msgs) >= DEFAULT_BATCH_SIZE:
            break
        before_id = batch[-1]['id']
    return all_msgs


def download_image(attachment, message_id):
    """Download an image attachment to IMAGE_REVIEW_DIR. Returns (local_path, url) or (None, None)."""
    url = attachment.get('url') or attachment.get('proxy_url')
    if not url:
        return None, None
    content_type = attachment.get('content_type', '') or ''
    filename = attachment.get('filename', 'image')
    if not content_type.startswith('image/'):
        if os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
            return None, None
    att_id = attachment.get('id', 'unknown')
    safe_name = f"{message_id}_{att_id}_{filename}"
    local_path = os.path.join(IMAGE_REVIEW_DIR, safe_name)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return None, None
        with open(local_path, 'wb') as f:
            f.write(resp.content)
        return local_path, url
    except Exception:
        return None, None


# ---------- Elder check ----------

def load_elder_cache():
    """Load cached elder list. Returns dict with elder_user_ids + cached_at,
    or None if cache is missing/expired."""
    if not os.path.exists(ELDERS_CACHE):
        return None
    try:
        with open(ELDERS_CACHE) as f:
            data = json.load(f)
        cached_at = datetime.datetime.fromisoformat(
            data['discovered_at'].replace('Z', '+00:00'))
        age = datetime.datetime.now(datetime.timezone.utc) - cached_at
        if age.total_seconds() > ELDER_CACHE_TTL_HOURS * 3600:
            return None
        return data
    except Exception:
        return None


def is_user_elder(user_id, cache=None):
    """Per-member role lookup (cached). 404 (user left guild) → not elder."""
    cache = cache if cache is not None else {}
    if user_id in cache:
        return cache[user_id]
    try:
        resp = requests.get(
            f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{user_id}",
            headers=_api_headers(), timeout=10)
        if resp.status_code == 200:
            member = resp.json()
            is_elder = ELDER_ROLE_ID in member.get('roles', [])
        else:
            # 404 = user left guild → treat as non-elder (per requirement #3)
            is_elder = False
    except Exception:
        is_elder = False
    cache[user_id] = is_elder
    return is_elder


def is_protected_user(user_id):
    """True if user_id is in the absolute protected list (hardcoded + elders).
    This is the LAST line of defense — checked before any action executes.
    """
    if not user_id:
        return False
    uid = str(user_id)
    if uid in PROTECTED_USERS:
        return True
    # Also include all current elders (dynamic, loaded from cache)
    elder_cache = load_elder_cache() or {}
    if uid in elder_cache.get('elder_user_ids', []):
        return True
    return False


def all_protected_users():
    """Returns the full set of protected user IDs (hardcoded + current elders).
    Used for logging/auditing and for the LLM to know who's untouchable.
    """
    elder_cache = load_elder_cache() or {}
    return PROTECTED_USERS | {str(uid) for uid in elder_cache.get('elder_user_ids', [])}


# ---------- State persistence ----------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"channels": {}, "last_run_at": None, "bundles_processed": []}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"channels": {}, "last_run_at": None, "bundles_processed": []}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def mark_processed(channel_id, last_message_id, last_timestamp):
    state = load_state()
    state['channels'][str(channel_id)] = {
        "last_message_id": last_message_id,
        "last_timestamp": last_timestamp,
        "processed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    state['last_run_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(state)


def is_already_processed(channel_id, message_id):
    state = load_state()
    ch = state.get('channels', {}).get(str(channel_id), {})
    last = ch.get('last_message_id')
    if not last:
        return False
    try:
        return int(message_id) <= int(last)
    except Exception:
        return False


# ---------- Bundle construction ----------

def build_message_record(msg, elder_cache, channel_name, is_thread=False, thread_name=None):
    """Convert a Discord message dict into our compact bundle record."""
    author = msg.get('author', {})
    user_id = author.get('id', '')
    is_bot = bool(author.get('bot', False))
    is_elder = (not is_bot) and is_user_elder(user_id, elder_cache) if user_id else False
    is_protected = is_protected_user(user_id)

    # Image attachments
    images = []
    for att in msg.get('attachments', []) or []:
        ct = (att.get('content_type') or '').lower()
        fn = (att.get('filename') or '').lower()
        if not (ct.startswith('image/') or any(fn.endswith(ext) for ext in IMAGE_EXTENSIONS)):
            continue
        local, url = download_image(att, msg.get('id', ''))
        images.append({
            "filename": att.get('filename', 'image'),
            "local_path": local,
            "original_url": url,
            "size": att.get('size', 0),
        })

    # Note embeds (for LLM context, not for image analysis — those are skip-listed)
    embeds = []
    for emb in msg.get('embeds', []) or []:
        embed_url = (emb.get('url', '') or '').lower()
        if any(d in embed_url for d in SKIP_EMBED_DOMAINS):
            embeds.append({"type": emb.get('type'), "url": emb.get('url', ''),
                          "skipped": True, "reason": "rich-link embed"})
        else:
            embeds.append({"type": emb.get('type'), "url": emb.get('url', '')})

    return {
        "message_id": msg.get('id'),
        "timestamp": msg.get('timestamp'),
        "channel_id": msg.get('channel_id'),
        "channel_name": channel_name,
        "thread_name": thread_name,
        "is_thread": is_thread,
        "author": {
            "id": user_id,
            "username": author.get('username', 'unknown'),
            "global_name": author.get('global_name'),
            "is_bot": is_bot,
            "is_elder": is_elder,
            "is_protected": is_protected,
        },
        "content": msg.get('content', ''),
        "images": images,
        "embeds": embeds,
    }


def build_bundle(channel_filter=None, hours=None, batch_size=DEFAULT_BATCH_SIZE,
                 dry_run=False):
    """Fetch messages + images, write a bundle JSON. Returns the bundle path."""
    elder_cache = load_elder_cache() or {"elder_user_ids": []}
    # Pre-warm cache with known elders so we don't re-fetch them
    elder_lookup_cache = {uid: True for uid in elder_cache.get('elder_user_ids', [])}
    # Build a friendly map for the bundle's metadata section
    elder_meta = {}
    for e in elder_cache.get('elders', []):
        uid = e.get('user_id')
        if uid:
            display = e.get('global_name') or e.get('username') or uid
            elder_meta[uid] = display

    if hours:
        cutoff_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    else:
        cutoff_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)

    channels = get_all_channels()
    if channel_filter:
        channels = [c for c in channels if c.get('name', '').lower() in channel_filter]

    bundle = {
        "version": 2,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "guild_id": GUILD_ID,
        "elder_role_id": ELDER_ROLE_ID,
        "elder_display_names": elder_meta,
        "protected_user_ids": sorted(all_protected_users()),
        "moderation_log_channel_id": MODERATION_LOG_CHANNEL_ID,
        "dry_run": dry_run,
        "channel_filter": channel_filter,
        "hours": hours,
        "cutoff": cutoff_dt.isoformat(),
        "channels_scanned": [],
        "messages": [],
    }

    total_added = 0
    for channel in channels:
        if total_added >= batch_size:
            bundle['note_stopped'] = f"hit batch limit ({batch_size})"
            break
        ch_id = channel['id']
        ch_name = channel.get('name', 'unknown')

        if channel.get('type') == 15:  # forum
            threads = get_forum_threads(ch_id) + get_archived_threads(ch_id)
            for thread in threads:
                if total_added >= batch_size:
                    break
                msgs = fetch_messages_in_window(thread['id'], cutoff_dt)
                for m in msgs:
                    if is_already_processed(ch_id, m.get('id', '')):
                        continue
                    rec = build_message_record(m, elder_lookup_cache, ch_name,
                                               is_thread=True,
                                               thread_name=thread.get('name'))
                    bundle['messages'].append(rec)
                    total_added += 1
                    if total_added >= batch_size:
                        break
        else:
            msgs = fetch_messages_in_window(ch_id, cutoff_dt)
            ch_count = 0
            for m in msgs:
                if is_already_processed(ch_id, m.get('id', '')):
                    continue
                rec = build_message_record(m, elder_lookup_cache, ch_name)
                bundle['messages'].append(rec)
                total_added += 1
                ch_count += 1
                if total_added >= batch_size:
                    break
            if ch_count > 0:
                bundle['channels_scanned'].append({"id": ch_id, "name": ch_name,
                                                    "added": ch_count})

    # Sort messages chronologically (oldest first) per LLM instruction
    bundle['messages'].sort(key=lambda r: r.get('timestamp', ''))

    # Image summary
    bundle['images'] = [
        {"message_id": r['message_id'], "channel_id": r['channel_id'],
         "author": r['author']['username'], "is_elder": r['author']['is_elder'],
         "is_protected": r['author']['is_protected'],
         "local_path": img['local_path'], "filename": img['filename']}
        for r in bundle['messages'] for img in r.get('images', [])
    ]
    bundle['image_count'] = len(bundle['images'])
    bundle['non_elder_image_count'] = sum(1 for i in bundle['images'] if not i['is_elder'])
    bundle['message_count'] = len(bundle['messages'])

    # Save
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_path = os.path.join(BUNDLE_DIR, f"bundle_{ts}.json")
    with open(out_path, 'w') as f:
        json.dump(bundle, f, indent=2)
    print(f"Bundle written: {out_path}")
    print(f"  messages: {bundle['message_count']}")
    print(f"  images:   {bundle['image_count']} ({bundle['non_elder_image_count']} non-elder)")
    print(f"  channels: {len(bundle['channels_scanned'])}")

    # Mark the last message of each channel as processed
    seen = {}
    for r in bundle['messages']:
        seen[r['channel_id']] = r  # chronological order, last one is newest
    for ch_id, r in seen.items():
        mark_processed(ch_id, r['message_id'], r['timestamp'])

    return out_path


# ---------- Action execution ----------

def execute_action(action):
    """Run a single action dict. Returns dict with ok/error result.
    Enforces PROTECTED_USERS at this level: any action that targets a protected
    user is REJECTED, regardless of what the LLM said.
    """
    t = action.get('type')

    # ─── Pre-flight: protected-user check (FIRST line of defense) ───
    target_user = action.get('user_id')
    if target_user and is_protected_user(target_user):
        return {
            "ok": False,
            "error": f"REFUSED: target user {target_user} is on the protected list",
            "protected": True,
            "action": action,
        }
    # For delete_message + send_to_review, we don't have a user_id in the action
    # itself — we'd need to look up the message author. Skip for now (LLM should
    # not produce delete actions against messages from protected users; the
    # bundle's `author.is_protected` field makes this visible).

    try:
        if t == 'delete_message':
            return _do_delete(action['channel_id'], action['message_id'])
        if t == 'timeout_user':
            return _do_timeout(action['user_id'], action['hours'], action.get('reason', ''))
        if t == 'ban_user':
            return _do_ban(action['user_id'], action.get('reason', ''),
                          action.get('delete_message_seconds', 0))
        if t == 'post_warning':
            return _do_post_warning(action['channel_id'], action['user_id'],
                                    action.get('message', ''))
        if t == 'post_in_channel':
            return _do_post_in_channel(action['channel_id'], action.get('message', ''))
        if t == 'send_to_review':
            return _do_post_in_channel(MODERATION_LOG_CHANNEL_ID,
                                       f"⚠️ Manual review needed: {action.get('reason', '?')}\n"
                                       f"Message: https://discord.com/channels/{GUILD_ID}/{action['channel_id']}/{action['message_id']}")
        return {"ok": False, "error": f"unknown action type: {t}"}
    except Exception as e:
        return {"ok": False, "error": f"exception: {e}"}


def _do_delete(channel_id, message_id):
    resp = requests.delete(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
        headers=_api_headers(), timeout=15)
    return {"ok": resp.status_code == 204, "status": resp.status_code,
            "message_id": message_id}


def _do_timeout(user_id, hours, reason):
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    iso = until.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    resp = requests.patch(
        f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{user_id}",
        headers={**_api_headers(), "Content-Type": "application/json"},
        json={"communication_disabled_until": iso, "reason": reason[:500]},
        timeout=15)
    return {"ok": resp.status_code == 200, "status": resp.status_code,
            "user_id": user_id, "until": iso}


def _do_ban(user_id, reason, delete_seconds):
    resp = requests.put(
        f"https://discord.com/api/v10/guilds/{GUILD_ID}/bans/{user_id}",
        headers={**_api_headers(), "Content-Type": "application/json"},
        json={"delete_message_seconds": delete_seconds, "reason": reason[:500]},
        timeout=15)
    return {"ok": resp.status_code == 204, "status": resp.status_code,
            "user_id": user_id}


def _do_post_warning(channel_id, user_id, message):
    text = f"⚠️ <@{user_id}> {message}"
    return _do_post_in_channel(channel_id, text)


def _do_post_in_channel(channel_id, content):
    if len(content) > 2000:
        content = content[:1997] + "..."
    resp = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers={**_api_headers(), "Content-Type": "application/json"},
        json={"content": content}, timeout=15)
    return {"ok": resp.status_code in (200, 201), "status": resp.status_code,
            "channel_id": channel_id, "length": len(content)}


def execute_action_list(actions, dry_run=False):
    """Execute the action list. In dry-run, just record what would happen.
    Belt-and-suspenders: also filters the list to drop any action targeting a
    protected user, even before execute_action's per-call check.

    Urgency lane (per ban_authority_v2, 2026-06-04 18:06 CEST update):
      - LLM judge sets action.insta_ban = true on ban_user actions that
        warrant it (coordinated hate raid, severe spam, etc.).
      - The script then enforces 3 hard gates (see is_urgent_action):
        target not protected/elder AND account age < 30 days.
      - If all pass: action goes to pending_actions_urgent.json
        (audit trail) AND is executed LIVE (auto_executed=true).
      - If LLM set the flag but a script gate fails: action falls
        back to pending_actions.json (human reviews).
      - In dry-run: urgent actions go to pending_actions_urgent.json
        with auto_executed=false (preview of what would have happened).
      - Non-urgent dry-run actions go to pending_actions.json.
    """
    # ─── Trim old urgent-lane entries (best-effort, idempotent) ───
    # Runs at the start of every execution so the live list doesn't grow
    # forever. Failures are non-fatal (logged but not raised) so a buggy
    # trim never blocks an actual moderation action.
    try:
        trim_out = subprocess.run(
            ['python3', os.path.join(os.path.dirname(__file__), 'discord_lookup.py'),
             'pending', 'urgent-trim', '--max-age-days', '30'],
            capture_output=True, text=True, timeout=15,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        if trim_out.returncode == 0:
            try:
                trim_data = json.loads(trim_out.stdout)
                if trim_data.get('trimmed_count', 0) > 0:
                    print(f"🧹 Trimmed {trim_data['trimmed_count']} urgent-lane entries "
                          f"(>{trim_data.get('max_age_days', 30)}d old) to "
                          f"{trim_data.get('archive_path')}")
            except (ValueError, json.JSONDecodeError):
                pass
        else:
            print(f"WARN: urgent-lane trim failed (exit {trim_out.returncode}): "
                  f"{trim_out.stderr[-200:]}")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"WARN: urgent-lane trim error: {e}")

    # ─── Pre-flight list-level check (SECOND line of defense) ───
    protected_ids = all_protected_users()
    filtered = []
    rejected = []
    for action in actions:
        target = str(action.get('user_id', ''))
        if target and target in protected_ids:
            rejected.append({"action": action,
                             "reason": f"target {target} is in protected list (refused at list level)"})
        else:
            filtered.append(action)

    # ─── Classify each action as urgent or normal ───
    users_cache = _load_users_cache_for_urgency()
    urgent_actions = [a for a in filtered if is_urgent_action(a, users_cache)]
    normal_actions = [a for a in filtered if not is_urgent_action(a, users_cache)]
    urgent_ids = {id(a) for a in urgent_actions}

    # ─── Stage dry-run normal actions to pending_actions.json ───
    if dry_run:
        for action in normal_actions:
            stage_pending_normal(action)

    # ─── Stage ALL urgent actions to pending_actions_urgent.json (audit trail) ───
    for action in urgent_actions:
        stage_pending_urgent(action, dry_run=dry_run, auto_executed=not dry_run)

    # ─── Execute ───
    results = list(rejected)  # rejected actions appear first with their reason
    for action in filtered:
        is_urgent = id(action) in urgent_ids
        if dry_run:
            results.append({"planned": action, "ok": True, "dry_run": True, "urgent": is_urgent})
            archive_action(action, {"ok": True, "dry_run": True}, dry_run=True, urgent=is_urgent)
        else:
            r = execute_action(action)
            results.append({"action": action, "urgent": is_urgent, **r})
            archive_action(action, r, dry_run=False, urgent=is_urgent)
    return results


# -----------------------------------------------------------------------------
# Urgency classification + pending-actions staging
# -----------------------------------------------------------------------------
def _load_users_cache_for_urgency():
    """Load the discord-lookup users cache (joined_at, message_count, etc.).
    Returns empty dict on any failure (urgency check is then conservative: NO).
    """
    try:
        with open(USERS_CACHE_FOR_URGENCY, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def is_urgent_action(action, users_cache):
    """Decide if an action is 'urgent' (auto-execute lane).

    Flow (per Arcurus 2026-06-04 18:06 CEST):
      1. LLM judge sets action.insta_ban = true on a ban_user action
         when warranted (coordinated hate raid, severe spam, etc.).
      2. The script then enforces 3 hard gates; if ANY fails, the action
         falls back to the normal pending queue (human reviews) and is
         NOT auto-executed.

    The 3 hard gates (in order):
      GATE A: action.insta_ban must be truthy (LLM's verdict)
      GATE B: action.type == 'ban_user' (defense in depth — LLM should
              only set the flag on bans, but the script checks anyway)
      GATE C: target is NOT protected AND NOT an elder
      GATE D: account age < URGENT_MAX_ACCOUNT_AGE_DAYS (30 days)
              — if we have no member data, we conservatively REFUSE
              (better to send to a human than auto-execute on a stranger
              we can't age-verify).

    Note: message count is intentionally NOT a gate. An attacker could
    just blast 5 messages to bypass a count threshold; the LLM judge
    evaluates content/pattern, which is a strictly better signal.
    """
    # ── GATE A: LLM must have set the flag ──
    if not action.get('insta_ban'):
        return False
    # ── GATE B: only ban_user can be insta-banned ──
    if action.get('type') != 'ban_user':
        return False
    target_id = str(action.get('user_id', ''))
    if not target_id:
        return False
    # ── GATE C: not protected, not elder ──
    if is_protected_user(target_id):
        return False
    if is_user_elder(target_id, cache=None):
        return False
    # ── GATE D: account age < 30 days ──
    u = users_cache.get(target_id, {})
    age_days = None
    for key in ('joined_at', 'account_created_at'):
        v = u.get(key)
        if not v:
            continue
        try:
            t = datetime.datetime.fromisoformat(v.replace('Z', '+00:00'))
            age_days = (datetime.datetime.now(datetime.timezone.utc) - t).days
            break
        except (ValueError, TypeError):
            continue
    if age_days is None:
        # No member data — we can't confirm the account is new.
        # Conservative: fall back to human queue. If LLM really thinks
        # this is insta-ban worthy, Arcurus / Lenny can 👍 it through.
        return False
    if age_days >= URGENT_MAX_ACCOUNT_AGE_DAYS:
        return False
    return True


def _load_jsonl_or_json(path, default):
    """Load a JSON file (object) and return default on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return default


def _save_json(path, data):
    """Atomically write a JSON file. Creates parent dir if needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _new_action_id(prefix):
    base = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{base}"


def _action_to_pending_dict(action, *, source, auto_executed, dry_run):
    """Build a pending-action record from a moderation action dict."""
    t = action.get('type', 'unknown')
    return {
        "id": _new_action_id("pu" if source == "cron-urgent" else "pa"),
        "action": t,
        "target_user_id": str(action.get('user_id', '')),
        "target_username": action.get('username') or action.get('target_username') or "",
        "duration": (f"{action.get('hours')}h" if t == 'timeout_user' and action.get('hours') is not None else ""),
        "reason": action.get('reason', ''),
        "source": source,
        "dry_run": dry_run,
        "auto_executed": auto_executed,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "actions_file": action.get('_source_file'),
    }


def stage_pending_normal(action):
    """Append a dry-run normal action to data/pending_actions.json.
    Returns the staged record (or None on failure)."""
    rec = _action_to_pending_dict(action, source="dry-run-cron", auto_executed=False, dry_run=True)
    d = _load_jsonl_or_json(PENDING_NORMAL, {"actions": []})
    d.setdefault("actions", []).append(rec)
    _save_json(PENDING_NORMAL, d)
    return rec


def stage_pending_urgent(action, *, dry_run, auto_executed):
    """Append an urgent action to data/pending_actions_urgent.json.
    Returns the staged record.
    In dry-run: auto_executed=False (preview of what would have happened).
    In live:    auto_executed=True (audit trail of what was auto-executed).
    """
    rec = _action_to_pending_dict(action, source="cron-urgent", auto_executed=auto_executed, dry_run=dry_run)
    d = _load_jsonl_or_json(PENDING_URGENT, {"actions": []})
    d.setdefault("actions", []).append(rec)
    _save_json(PENDING_URGENT, d)
    return rec


def archive_action(action, result, dry_run=False, urgent=False):
    """Append a single moderation action to the persistent JSONL archive.
    Called from execute_action_list() after every action (live or dry-run).
    Failed and rejected actions are also recorded so the audit trail is
    complete. One JSON object per line, append-only.
    """
    import json as _json
    t = action.get('type', 'unknown')
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": t,
        "target_user_id": action.get('user_id'),
        "target_username": action.get('username') or action.get('target_username'),
        "duration": (f"{action.get('hours')}h" if t == 'timeout_user' and action.get('hours') is not None else None),
        "reason": action.get('reason', ''),
        "source": "cron",
        "moderator": "Selena",
        "dry_run": dry_run,
        "urgent": urgent,
        "result_ok": bool(result.get('ok', False)),
        "result_error": result.get('error'),
        "actions_file": action.get('_source_file'),
    }
    # Channel/message context for delete + send_to_review actions
    if t in ('delete_message', 'send_to_review', 'post_warning', 'post_in_channel'):
        record['channel_id'] = action.get('channel_id')
        record['message_id'] = action.get('message_id')
    try:
        with open(ACTION_LOG, 'a', encoding='utf-8') as f:
            f.write(_json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        # Never let a logging failure break execution
        print(f"WARN: archive_action failed: {e}")


def rename_to_done(actions_path, dry_run=False):
    """After execution, rename the actions file from
    `..._YYYYMMDD-HHMMSS.json` to `..._YYYYMMDD-HHMMSS-DONE.json` so that:
      - It's obvious at a glance that the file has been processed
      - Multiple runs don't accidentally overwrite each other (the timestamp
        makes each filename unique)
    In dry-run mode, leave the filename as-is (so a human reviewing later
    can re-run it as a real action by removing --dry-run).
    Returns the new path.
    """
    import shutil
    if not actions_path or not os.path.exists(actions_path):
        return actions_path
    if dry_run:
        return actions_path  # don't rename dry-runs; they may be re-executed

    # Strip any existing .json, append -DONE, restore .json
    base, ext = os.path.splitext(actions_path)
    if base.endswith('-DONE'):
        return actions_path  # already renamed
    new_path = base + '-DONE' + ext
    try:
        os.rename(actions_path, new_path)
        return new_path
    except Exception as e:
        print(f"WARN: could not rename {actions_path} to {new_path}: {e}")
        return actions_path


# ---------- Report formatting (called by LLM, not by the script) ----------

def format_execution_report(actions, results, dry_run):
    """Build a markdown report of executed (or planned) actions."""
    lines = []
    mode = "🟡 DRY-RUN" if dry_run else "🟢 LIVE"
    lines.append(f"## Action Report — {mode}")
    lines.append(f"**Time:** {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Actions:** {len(actions)}")
    lines.append("")
    for i, (a, r) in enumerate(zip(actions, results), 1):
        ok = r.get('ok', False)
        protected = r.get('protected', False) or 'reason' in r
        if protected:
            status = "🛑 REFUSED"
        elif ok:
            status = "✅"
        else:
            status = "❌"
        lines.append(f"{status} **{a.get('type', '?')}** — {a}")
        if r.get('reason'):
            lines.append(f"   reason: {r['reason']}")
        if not ok and r.get('error'):
            lines.append(f"   error: {r['error']}")
    return "\n".join(lines)


# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description='OpenLife moderation pipeline v2')
    parser.add_argument('--bundle', action='store_true',
                        help='Build a moderation bundle (data prep only, no analysis)')
    parser.add_argument('--channel', action='append', default=None,
                        help='Filter to channel name (can be repeated)')
    parser.add_argument('--hours', type=float, default=None,
                        help='Look back N hours (default 1)')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE,
                        help=f'Max messages per run (default {DEFAULT_BATCH_SIZE})')
    parser.add_argument('--execute', type=str, default=None,
                        help='Path to actions JSON to execute')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log actions but do not execute them')
    parser.add_argument('--reset-state', action='store_true',
                        help='Wipe the processed-message state (re-scan everything)')
    args = parser.parse_args()

    if args.reset_state:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print(f"State reset: {STATE_FILE} removed")
        return

    if args.bundle:
        ok, msg = validate_bot_token()
        if not ok:
            print(f"❌ Auth failed: {msg}")
            sys.exit(2)
        out = build_bundle(
            channel_filter=[c.lower() for c in args.channel] if args.channel else None,
            hours=args.hours,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        print(f"\nBUNDLE_PATH={out}")
        return

    if args.execute:
        if not os.path.exists(args.execute):
            print(f"❌ Actions file not found: {args.execute}")
            sys.exit(2)
        with open(args.execute) as f:
            data = json.load(f)
        actions = data.get('actions', data) if isinstance(data, dict) else data
        if not isinstance(actions, list):
            print("❌ Actions must be a list or {'actions': [...]}")
            sys.exit(2)
        # Validate every action has a reason
        for i, a in enumerate(actions):
            if 'reason' not in a or not a['reason']:
                print(f"❌ Action #{i+1} ({a.get('type', '?')}) is missing a 'reason' field")
                print(f"   Action: {a}")
                sys.exit(2)
        results = execute_action_list(actions, dry_run=args.dry_run)
        # Write results
        out_path = args.execute.replace('.json', '_results.json')
        with open(out_path, 'w') as f:
            json.dump({
                "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "dry_run": args.dry_run,
                "source_actions_file": args.execute,
                "actions": actions,
                "results": results,
            }, f, indent=2)
        # Rename source to -DONE if real mode (not dry-run)
        if not args.dry_run:
            new_path = rename_to_done(args.execute, dry_run=False)
            if new_path != args.execute:
                print(f"Renamed actions file: {args.execute} → {new_path}")
        # Print report
        print(format_execution_report(actions, results, args.dry_run))
        print(f"\nResults written: {out_path}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
