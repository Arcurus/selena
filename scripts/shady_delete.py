#!/usr/bin/env python3
"""
Delete Shady's off-topic messages (not in #offtopic) or flame war messages
Check last 24 hours across all channels
"""

import os
import json
import datetime
import requests

# Configuration
SERVER_ID = "985997281734041680"
SHADY_USER_IDS = ["1102426821405982722", "673401181023895572"]
BOT_TOKEN = None

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
    "bug-report": {
        "allow_offtopic": False,
        "allow_spam": False,
        "description": "Bug reports - should be on topic"
    },
    "questions": {
        "allow_offtopic": False,
        "allow_spam": False,
        "description": "Questions - should be on topic"
    }
}

# Flame war keywords
FLAME_WAR_PATTERNS = [
    "you're stupid", "you are stupid", "shut up", "stfu", "idiot", 
    "moron", "dumb", "loser", "pathetic", "ignorant", "incompetent",
    "you don't know", "you don't understand", "you're wrong", "you are wrong",
    "give up", "quit", "stop trying", "embarrassing",
    "waste of space", "waste", "bs", "bullshit", "ffs", "for god's sake",
    "get me banned", "you're going to get", "can you stop", "stop dude",
    "shits not even", "not even funny"
]

SEVERE_PATTERNS = [
    "卐", "卍",  # Nazi/offensive symbols
]

def load_config():
    global BOT_TOKEN
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    with open(config_path) as f:
        config = json.load(f)
    BOT_TOKEN = config.get('channels', {}).get('discord', {}).get('token', '')

def get_headers():
    return {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

def get_all_channels():
    url = f"https://discord.com/api/v10/guilds/{SERVER_ID}/channels"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code != 200:
        print(f"Failed to get channels: {resp.status_code}")
        return []
    channels = resp.json()
    # Filter to text channels (type 0) and categories (type 4), exclude voice (type 2)
    return [c for c in channels if c['type'] in [0, 4, 15]]  # 15 = forum

def get_channel_name(channel_id):
    url = f"https://discord.com/api/v10/channels/{channel_id}"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        return resp.json().get('name', 'unknown')
    return 'unknown'

def get_messages(channel_id, limit=100):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    params = {"limit": min(limit, 100)}
    resp = requests.get(url, headers=get_headers(), params=params)
    if resp.status_code != 200:
        return []
    return resp.json()

def get_forum_threads(channel_id):
    url = f"https://discord.com/api/v10/channels/{channel_id}/threads"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        return resp.json().get('threads', [])
    return []

def get_archived_threads(channel_id):
    url = f"https://discord.com/api/v10/channels/{channel_id}/threads/archived/public"
    resp = requests.get(url, headers=get_headers())
    if resp.status_code == 200:
        return resp.json().get('threads', [])
    return []

def get_thread_messages(thread_id, limit=50):
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    params = {"limit": min(limit, 50)}
    resp = requests.get(url, headers=get_headers(), params=params)
    if resp.status_code == 200:
        return resp.json()
    return []

def analyze_message(msg, channel_name):
    """Returns (is_problem, reason)"""
    content = msg.get('content', '').lower()
    author_id = str(msg.get('author', {}).get('id', ''))
    
    # Only process Shady's messages
    if author_id not in SHADY_USER_IDS:
        return False, None
    
    # Get channel rules
    channel_lower = channel_name.lower()
    rules = CHANNEL_RULES.get(channel_lower, {"allow_offtopic": False, "allow_spam": False})
    
    problems = []
    
    # Check for flame war engagement
    for pattern in FLAME_WAR_PATTERNS:
        if pattern in content:
            problems.append(f"flame war: '{pattern}'")
    
    # Check for severe/offensive patterns (always checked)
    for pattern in SEVERE_PATTERNS:
        if pattern in content:
            problems.append(f"offensive symbol: '{pattern}'")
    
    # Off-topic check
    if not rules.get("allow_offtopic", False):
        # Check if it's off-topic (very short messages, gibberish, or random topics)
        if len(content) < 10 and content.strip():
            problems.append("very short off-topic message")
        elif len(content) > 5:
            # Check for obviously off-topic content
            off_topic_indicators = ["w/", " btw", " btw", " lol", " lol", " lmao", 
                                   " just for", " random", " btw", " fyi", " fwiw"]
            for indicator in off_topic_indicators:
                if indicator in content:
                    problems.append(f"potentially off-topic: '{indicator}'")
    
    if problems:
        return True, "; ".join(problems)
    return False, None

def delete_message(channel_id, message_id):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    resp = requests.delete(url, headers=get_headers())
    return resp.status_code in [200, 204]

def main():
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] Checking Shady's messages from last 24 hours...")
    
    load_config()
    
    # Validate token
    test_url = f"https://discord.com/api/v10/guilds/{SERVER_ID}"
    resp = requests.get(test_url, headers=get_headers())
    if resp.status_code == 401:
        print("❌ AUTHENTICATION FAILED: Bot token is invalid or expired")
        return
    if resp.status_code != 200:
        print(f"API returned status {resp.status_code}")
        return
    
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    print(f"Cutoff time: {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")
    
    channels = get_all_channels()
    print(f"Checking {len(channels)} channels/forum areas...")
    
    all_problems = []
    
    for channel in channels:
        channel_id = channel['id']
        channel_name = channel['name']
        channel_type = channel['type']
        
        if channel_type == 15:  # Forum
            print(f"Checking forum #{channel_name}...")
            threads = get_forum_threads(channel_id)
            threads.extend(get_archived_threads(channel_id))
            for thread in threads:
                thread_name = thread.get('name', 'unnamed')
                messages = get_thread_messages(thread['id'], limit=50)
                for msg in messages:
                    msg_time = datetime.datetime.fromisoformat(msg['timestamp'].replace('Z', '+00:00'))
                    if msg_time < cutoff:
                        continue
                    is_problem, reason = analyze_message(msg, channel_name)
                    if is_problem:
                        all_problems.append({
                            'channel_id': channel_id,
                            'thread_id': thread['id'],
                            'thread_name': thread_name,
                            'channel_name': channel_name,
                            'message_id': msg['id'],
                            'content': msg.get('content', '')[:100],
                            'author': msg.get('author', {}).get('username', 'unknown'),
                            'reason': reason,
                            'timestamp': msg['timestamp']
                        })
        elif channel_type == 0:  # Text channel
            messages = get_messages(channel_id, limit=100)
            for msg in messages:
                msg_time = datetime.datetime.fromisoformat(msg['timestamp'].replace('Z', '+00:00'))
                if msg_time < cutoff:
                    continue
                is_problem, reason = analyze_message(msg, channel_name)
                if is_problem:
                    all_problems.append({
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'message_id': msg['id'],
                        'content': msg.get('content', '')[:100],
                        'author': msg.get('author', {}).get('username', 'unknown'),
                        'reason': reason,
                        'timestamp': msg['timestamp']
                    })
    
    print(f"\nFound {len(all_problems)} problematic messages from Shady")
    
    if not all_problems:
        print("No off-topic or flame war messages found. Done.")
        return
    
    print("\nProblematic messages:")
    for i, p in enumerate(all_problems, 1):
        thread_info = f" in thread #{p.get('thread_name', 'unknown')}" if 'thread_name' in p else ""
        print(f"{i}. #{p['channel_name']}{thread_info}: {p['content']}")
        print(f"   Reason: {p['reason']} | {p['timestamp']}")
    
    # Ask before deleting
    print(f"\nDelete all {len(all_problems)} messages? (y/n)")
    response = input("> ").strip().lower()
    
    if response == 'y':
        deleted = 0
        for p in all_problems:
            # Determine which channel to delete from
            delete_channel_id = p.get('thread_id', p['channel_id'])
            if delete_message(delete_channel_id, p['message_id']):
                deleted += 1
                print(f"Deleted: #{p['channel_name']} - {p['content'][:50]}...")
            else:
                print(f"Failed to delete: #{p['channel_name']} - {p['content'][:50]}...")
        print(f"\nDeleted {deleted}/{len(all_problems)} messages")
    else:
        print("Aborted. No messages deleted.")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--yes':
        # Auto-confirm deletion
        original_input = input
        def mock_input(prompt=''):
            print(prompt + 'y')
            return 'y'
        import builtins
        builtins.input = mock_input
    main()
