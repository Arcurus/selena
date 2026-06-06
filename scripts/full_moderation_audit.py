#!/usr/bin/env python3
"""
Full 24-Hour Moderation Audit for Open Life Reborn
Checks all channels for problematic content and user behavior
"""

import os
import json
import datetime
import requests
from collections import defaultdict

# Config
SERVER_ID = "985997281734041680"
BOT_TOKEN = None

# Problematic users to watch
PROBLEMATIC_USERS = {
    "1102426821405982722": {"name": "birderydev", "alts": ["birderydev", "birderyaaz", "Shady"], "severity": "high"},
    "673401181023895572": {"name": "birderyaaz", "alts": ["birderydev", "birderyaaz", "Shady"], "severity": "high"},
    "1101322508490997770": {"name": "moon__r", "alts": ["moon__r"], "severity": "high"},
    "1065666569732116480": {"name": "ovulasaoflangoherobraine", "alts": ["ovulasaoflangoherobraine", "Misterio Herobraine"], "severity": "critical"},
}

# General problem keywords
PROBLEM_KEYWORDS = [
    "dumb", "stupid", "idiot", "retard", "moron", "loser",
    "your code is shit", "mental illness", "go for a full punt",
    "eat your banana", "BLACK MONKEY", "doxxing", "dox",
]

# Conflict bait patterns
BAIT_PATTERNS = [
    "really?", "sure.", "nice.", "lol.", "lmao.", "nice one.",
    "🤨", "😂", "bro really", "bro thinks", "ok bro",
    "calm down", "chill", "seriously?", "wow.",
]

def load_config():
    global BOT_TOKEN
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    with open(config_path) as f:
        config = json.load(f)
    BOT_TOKEN = config['channels']['discord']['token']

def get_all_channels():
    """Get all text channels in the server"""
    url = f"https://discord.com/api/v10/guilds/{SERVER_ID}/channels"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        print(f"Error getting channels: {resp.status_code} {resp.text}")
        return []
    
    channels = resp.json()
    text_channels = [ch for ch in channels if ch['type'] == 0]
    return text_channels

def get_channel_messages(channel, hours=24):
    """Get messages from a channel for the last N hours"""
    # Calculate timestamp for N hours ago
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    url = f"https://discord.com/api/v10/channels/{channel['id']}/messages?limit=100"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    
    all_messages = []
    while True:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            break
        
        messages = resp.json()
        if not messages:
            break
        
        # Check if oldest message is before cutoff
        oldest_time = datetime.datetime.fromisoformat(messages[-1]['timestamp'].replace('Z', '+00:00'))
        if oldest_time < cutoff:
            # Only keep messages within cutoff
            all_messages.extend([m for m in messages if datetime.datetime.fromisoformat(m['timestamp'].replace('Z', '+00:00')) >= cutoff])
            break
        
        all_messages.extend(messages)
        
        # Get last message timestamp for pagination
        before = messages[-1]['id']
        url = f"https://discord.com/api/v10/channels/{channel['id']}/messages?limit=100&before={before}"
    
    return all_messages

def analyze_message(msg, user_info=None):
    """Analyze a message for problems"""
    content = msg.get('content', '').lower()
    author = msg['author']
    problems = []
    severity = "low"
    
    # Check if from known problematic user
    if author['id'] in PROBLEMATIC_USERS:
        info = PROBLEMATIC_USERS[author['id']]
        if info['severity'] == 'critical':
            problems.append(f"⚠️ BLOCKED USER ({info['name']})")
            severity = "critical"
        elif info['severity'] == 'high':
            problems.append(f"🔴 WATCHED USER ({info['name']})")
            severity = "high"
    
    # Check for problem keywords
    for keyword in PROBLEM_KEYWORDS:
        if keyword in content:
            problems.append(f"Problematic language: '{keyword}'")
            if severity != "critical":
                severity = "medium"
    
    # Check for conflict bait
    for pattern in BAIT_PATTERNS:
        if pattern.lower() in content:
            problems.append(f"Possible conflict bait: '{pattern}'")
            if severity not in ["critical", "high"]:
                severity = "medium"
    
    # Check for gibberish (very short random characters)
    if len(content) > 0 and len(content) < 5:
        if not any(c.isalnum() for c in content):
            problems.append("Gibberish/spam")
            if severity not in ["critical", "high", "medium"]:
                severity = "low"
    
    # Check for long repetitive messages (spam)
    if len(content) > 500:
        # Check for repetitive patterns
        words = content.split()
        if len(words) > 10:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.3:
                problems.append("Possible spam (repetitive)")
                severity = "medium"
    
    return problems, severity

def generate_report():
    """Generate full moderation report"""
    load_config()
    
    print("=" * 60)
    print("FULL 24-HOUR MODERATION AUDIT - Open Life Reborn")
    print(f"Started: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    
    channels = get_all_channels()
    print(f"\nFound {len(channels)} channels to check\n")
    
    # Store all problematic messages
    all_issues = []
    channel_stats = defaultdict(lambda: {"total": 0, "problematic": 0, "users": set()})
    user_stats = defaultdict(lambda: {"messages": 0, "issues": []})
    
    for channel in channels:
        print(f"Checking #{channel['name']}...", end=" ")
        
        messages = get_channel_messages(channel, hours=24)
        channel_stats[channel['name']]['total'] = len(messages)
        
        channel_issues = []
        
        for msg in messages:
            author = msg['author']
            channel_stats[channel['name']]['users'].add(author['username'])
            
            # Track user stats
            user_stats[author['username']]['messages'] += 1
            
            # Analyze message
            problems, severity = analyze_message(msg)
            
            if problems or severity in ["high", "critical"]:
                issue = {
                    'channel': channel['name'],
                    'channel_id': channel['id'],
                    'author': author['username'],
                    'author_id': author['id'],
                    'content': msg.get('content', '')[:200],
                    'full_content': msg.get('content', ''),
                    'timestamp': msg['timestamp'],
                    'problems': problems,
                    'severity': severity,
                    'message_id': msg['id']
                }
                
                channel_issues.append(issue)
                all_issues.append(issue)
                user_stats[author['username']]['issues'].append(issue)
                
                if severity in ["high", "critical"]:
                    channel_stats[channel['name']]['problematic'] += 1
        
        print(f"found {len(channel_issues)} issues")
    
    # Generate report
    report = []
    report.append("=" * 60)
    report.append("📋 FULL 24-HOUR MODERATION AUDIT REPORT")
    report.append(f"Server: Open Life Reborn")
    report.append(f"Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    report.append("=" * 60)
    
    # Summary stats
    report.append("\n📊 **CHANNEL ACTIVITY SUMMARY:**\n")
    total_messages = sum(s['total'] for s in channel_stats.values())
    total_users = len(set(u for s in channel_stats.values() for u in s['users']))
    report.append(f"- Total messages (24h): {total_messages}")
    report.append(f"- Total active users: {total_users}")
    report.append(f"- Total problematic messages: {len(all_issues)}")
    
    # High priority issues
    high_issues = [i for i in all_issues if i['severity'] in ['high', 'critical']]
    if high_issues:
        report.append(f"\n⚠️ **HIGH PRIORITY ISSUES ({len(high_issues)}):**\n")
        for issue in high_issues:
            report.append(f"**#{issue['channel']}** - {issue['author']} ({issue['timestamp']})")
            report.append(f"> {issue['content'][:150]}...")
            for prob in issue['problems']:
                report.append(f"  - {prob}")
            report.append("")
    
    # Problematic users breakdown
    report.append("\n👥 **PROBLEMATIC USERS:**\n")
    problematic_users = {k: v for k, v in user_stats.items() if v['issues']}
    if problematic_users:
        for username, stats in sorted(problematic_users.items(), key=lambda x: -len(x[1]['issues'])):
            report.append(f"**{username}**: {stats['messages']} messages, {len(stats['issues'])} flagged")
            for issue in stats['issues']:
                report.append(f"  - #{issue['channel']}: {issue['problems'][:2]}")
    else:
        report.append("No problematic users detected.")
    
    # All issues by channel
    report.append("\n📍 **ISSUES BY CHANNEL:**\n")
    issues_by_channel = defaultdict(list)
    for issue in all_issues:
        issues_by_channel[issue['channel']].append(issue)
    
    for channel, issues in sorted(issues_by_channel.items(), key=lambda x: -len(x[1])):
        if issues:
            report.append(f"**#{channel}**: {len(issues)} issue(s)")
            for issue in issues[:3]:  # Show first 3 per channel
                report.append(f"  - [{issue['severity'].upper()}] {issue['author']}: {issue['content'][:80]}...")
            if len(issues) > 3:
                report.append(f"  - ...and {len(issues) - 3} more")
    
    # Recommendations
    report.append("\n💡 **RECOMMENDATIONS:**\n")
    
    if any(i['severity'] == 'critical' for i in all_issues):
        report.append("1. ⚠️ URGENT: Blocked user(s) were active - verify bans are enforced")
    
    if len([i for i in all_issues if 'conflict bait' in str(i['problems'])]) > 3:
        report.append("2. 🔴 High conflict bait activity - consider posting conflict de-escalation guidelines")
    
    watched_users = [k for k, v in user_stats.items() if any(i['severity'] == 'high' for i in v['issues'])]
    if watched_users:
        report.append(f"3. 🟠 Watch these users: {', '.join(watched_users)}")
        report.append("   - Consider issuing formal warnings")
    
    if len(all_issues) > 20:
        report.append("4. 🟡 High issue volume - consider more active moderation presence")
    elif len(all_issues) < 5:
        report.append("4. ✅ Low issue volume - community is behaving well")
    
    report.append("\n" + "=" * 60)
    
    return "\n".join(report), all_issues, channel_stats, user_stats

if __name__ == "__main__":
    report, issues, channel_stats, user_stats = generate_report()
    print("\n" + report)
    
    # Also save to file
    with open("/tmp/moderation_audit_report.txt", "w") as f:
        f.write(report)
    print("\nReport saved to /tmp/moderation_audit_report.txt")
