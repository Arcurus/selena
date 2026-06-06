#!/usr/bin/env python3
"""
Shady Monitor Script - Channel-Aware Version
Runs hourly to check for Shady's messages across all channels in Open Life Reborn
Gives feedback based on channel context (off-topic is fine in #offtopic, not in #general)
"""

import os
import json
import datetime
import requests

# Configuration
SERVER_ID = "985997281734041680"
GENERAL_CHANNEL_ID = "985997281734041683"
SHADY_USER_IDS = ["1102426821405982722", "673401181023895572"]  # birderydev, birderyaaz
BOT_TOKEN = None

# Channel-specific rules (same as moderation_check.py)
CHANNEL_RULES = {
    "offtopic": {
        "allow_offtopic": True,
        "allow_spam": False,
        "description": "Off-topic chat allowed"
    },
    "general": {
        "allow_offtopic": False,
        "allow_spam": False,
        "allow_gibberish": False,
        "description": "General discussion - should be on topic"
    },
    "pictures": {
        "allow_offtopic": True,
        "allow_spam": False,
        "allow_gibberish": False,
        "description": "Images/pictures"
    },
    "stories": {
        "allow_offtopic": True,
        "allow_spam": False,
        "allow_offensive": False,
        "description": "Creative writing/stories"
    },
    "coding": {
        "allow_offtopic": True,
        "allow_spam": False,
        "description": "Coding discussion"
    },
    "default": {
        "allow_offtopic": False,
        "allow_spam": False,
        "allow_gibberish": False,
        "description": "Default rules"
    }
}

# On-topic keywords
ONTOPIC_KEYWORDS = [
    'openlife', 'ohol', 'game', 'server', 'player', 'bug', 'update',
    'community', 'code', 'programming', 'project', 'help', 'question',
    'playing', 'steam', 'mod', 'modding', 'hunt', 'hunter'
]

def load_config():
    global BOT_TOKEN
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    with open(config_path) as f:
        config = json.load(f)
    BOT_TOKEN = config['channels']['discord']['token']

def get_all_channels():
    url = f"https://discord.com/api/v10/guilds/{SERVER_ID}/channels"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        return []
    channels = resp.json()
    return [ch for ch in channels if ch['type'] == 0]

def get_channel_rules(channel_name):
    channel_name = channel_name.lower()
    for key, rules in CHANNEL_RULES.items():
        if key in channel_name:
            return rules
    return CHANNEL_RULES["default"]

def check_channel_for_shady(channel):
    url = f"https://discord.com/api/v10/channels/{channel['id']}/messages?limit=20"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    resp = requests.get(url, headers=headers)
    
    if resp.status_code != 200:
        return None
    
    messages = resp.json()
    shady_messages = []
    
    for msg in messages:
        if msg['author']['id'] in SHADY_USER_IDS:
            msg_time = datetime.datetime.fromisoformat(msg['timestamp'].replace('Z', '+00:00'))
            now = datetime.datetime.now(datetime.timezone.utc)
            age_seconds = (now - msg_time).total_seconds()
            
            if age_seconds <= 3900:
                shady_messages.append({
                    'id': msg['id'],
                    'content': msg.get('content', ''),
                    'author': msg['author']['username'],
                    'channel_id': channel['id'],
                    'channel_name': channel['name'],
                    'timestamp': msg['timestamp'],
                    'age_minutes': age_seconds / 60
                })
    
    return shady_messages

def analyze_message(msg, rules):
    """Analyze a Shady message considering channel context."""
    content = msg['content'].lower()
    content_raw = msg['content']
    problems = []
    
    # Check for personal attacks (always bad)
    attack_patterns = ['your code is', 'you have a mental', 'go for a full punt', 
                       'shut up', 'stupid', 'idiot', 'dumb', 'waste of space',
                       "you're an actual"]
    for pattern in attack_patterns:
        if pattern in content:
            problems.append(f"Personal attack ({pattern})")
    
    # Check for severe violations (always bad)
    severe_patterns = ['doxx', 'dox', 'leak', 'racist', 'nazi', '卐', '卍']
    for pattern in severe_patterns:
        if pattern in content:
            problems.append(f"Severe violation ({pattern})")
    
    # Check for conflict bait
    bait_patterns = ['really?', 'sure.', 'nice.', 'lol.', 'lmao.', 'nice one.',
                    '🤨', '😂', 'bro really', 'bro thinks', 'ok bro', 'calm down',
                    'seriously?', 'wow.', 'whatever.', 'ok then.', 'sure you are.']
    for keyword in bait_patterns:
        if keyword in content:
            problems.append(f"Conflict bait ('{keyword}')")
            break
    
    # Check for gibberish
    if len(content) < 5 and len(content) > 0 and not any(c.isalnum() for c in content):
        if not rules.get("allow_gibberish", False):
            problems.append("Gibberish/spam")
    
    # Check for off-topic (only if channel doesn't allow it)
    if not rules.get("allow_offtopic", False):
        has_topic = any(kw in content for kw in ONTOPIC_KEYWORDS)
        if len(content_raw) < 20 and not has_topic:
            problems.append("Off-topic content")
    
    # Check for repetitive/annoying short messages
    if len(content_raw) < 10 and content_raw.strip():
        if not rules.get("allow_offtopic", False):
            problems.append("Very short message (off-topic in strict channel)")
    
    return problems

def delete_message(channel_id, message_id):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    resp = requests.delete(url, headers=headers)
    return resp.status_code == 204

def timeout_user(user_id, duration_hours, reason):
    timeout_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=duration_hours)
    iso_time = timeout_until.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    
    url = f"https://discord.com/api/v10/guilds/{SERVER_ID}/members/{user_id}"
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    data = {"communication_disabled_until": iso_time, "reason": reason[:500]}
    
    try:
        resp = requests.patch(url, headers=headers, json=data, timeout=10)
        return resp.status_code == 200
    except:
        return False

def post_feedback_in_channel(msg, problems):
    """Post feedback directly in the channel where Shady posted."""
    channel_id = msg['channel_id']
    
    preview = msg['content'][:100] + "..." if len(msg['content']) > 100 else msg['content']
    if not preview.strip():
        preview = "[attachment/image]"
    
    header = f"📋 **Feedback — {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M UTC')}**\n\n"
    header += f"> {preview}\n\n"
    header += f"**Channel:** #{msg['channel_name']}\n"
    header += f"**Issues:** {', '.join(problems)}\n"
    header += "\n**Feedback:**\n"
    
    for problem in problems:
        if 'personal attack' in problem.lower():
            header += "❌ **Personal attacks are strictly prohibited.** Keep discussions respectful.\n"
        elif 'severe violation' in problem.lower():
            header += "🚫 **Severe violation detected.** This will result in immediate timeout.\n"
        elif 'conflict bait' in problem.lower():
            header += "⚠️ **Do NOT bait other users into conflict.** Sarcasm and provocation are not welcome.\n"
        elif 'off-topic' in problem.lower():
            header += f"💬 **Off-topic content.** This channel is for *{msg.get('rules_desc', 'on-topic discussion')}*. Use #off-topic for general chat.\n"
        elif 'gibberish' in problem.lower():
            header += "🔇 **Gibberish and spam are not appropriate** in this channel.\n"
        elif 'short message' in problem.lower():
            header += "💬 **Very short messages** that aren't responses are considered off-topic here.\n"
        else:
            header += f"⚠️ {problem}\n"
    
    header += "\n*Next violation = longer timeout. — Selena 🌙*"
    
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    data = {"content": header}
    
    resp = requests.post(url, headers=headers, json=data)
    return resp.status_code == 200

def validate_bot_token():
    """Validate that the bot token works. Returns (success, error_message)."""
    if not BOT_TOKEN:
        return False, "No bot token found in config"
    
    # Test with a simple API call
    url = f"https://discord.com/api/v10/guilds/{SERVER_ID}"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 401:
            return False, "❌ AUTHENTICATION FAILED: Bot token is invalid or expired (401 Unauthorized)"
        if resp.status_code == 403:
            return False, "❌ ACCESS DENIED: Bot lacks permissions (403 Forbidden)"
        if resp.status_code != 200:
            return False, f"API returned status {resp.status_code}"
        return True, None
    except Exception as e:
        return False, f"API call failed: {str(e)}"

def main():
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] Running Shady monitor...")
    
    load_config()
    
    # Validate bot token first - fail fast if auth is broken
    auth_ok, auth_error = validate_bot_token()
    if not auth_ok:
        print(f"AUTHENTICATION ERROR: {auth_error}")
        print("Shady monitor FAILED - cannot proceed without valid authentication")
        return
    
    channels = get_all_channels()
    print(f"Checking {len(channels)} channels...")
    
    all_shady_messages = []
    
    for channel in channels:
        result = check_channel_for_shady(channel)
        if result:
            all_shady_messages.extend(result)
    
    print(f"Found {len(all_shady_messages)} total Shady messages")
    
    if not all_shady_messages:
        print("No Shady messages in the last hour. Done.")
        return
    
    summary_messages = []
    deleted_count = 0
    timeout_count = 0
    
    for msg in all_shady_messages:
        msg['time_str'] = datetime.datetime.fromisoformat(
            msg['timestamp'].replace('Z', '+00:00')
        ).strftime('%H:%M')
        
        rules = get_channel_rules(msg['channel_name'])
        msg['rules_desc'] = rules['description']
        
        problems = analyze_message(msg, rules)
        msg['problems'] = problems
        
        # Create preview
        preview = msg['content'][:100] + "..." if len(msg['content']) > 100 else msg['content']
        if not preview.strip():
            preview = "[attachment/image]"
        msg['preview'] = preview
        
        # Decide action based on severity
        if problems:
            is_severe = any('severe violation' in p.lower() or 'personal attack' in p.lower() 
                          for p in problems)
            
            if is_severe:
                # Delete + timeout
                if delete_message(msg['channel_id'], msg['id']):
                    deleted_count += 1
                    msg['deleted'] = True
                timeout_user("1102426821405982722", 168, f"Severe violation: {', '.join(problems)}")
                timeout_count += 1
                msg['action'] = "DELETED + 1 WEEK TIMEOUT"
            else:
                # Just warn in channel
                post_feedback_in_channel(msg, problems)
                msg['action'] = "WARNING POSTED"
            
            summary_messages.append(msg)
    
    print(f"Processed {len(summary_messages)} messages")
    print(f"Deleted: {deleted_count}, Timed out: {timeout_count}")
    print(f"Done at {datetime.datetime.now(datetime.timezone.utc).isoformat()}")

if __name__ == "__main__":
    main()
