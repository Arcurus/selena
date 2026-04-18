#!/usr/bin/env python3
"""
Selena v2 - Basic Agent Loop
============================

Implements the context -> call -> process loop.

Usage:
    python3 agent_loop.py task "What should I work on next?"
"""

import os
import sys
import json
import datetime
from typing import List, Dict, Optional, Any

# Add code directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configuration
AGENT_ROOT = os.path.expanduser("~/openclaw/workspace/selena")
MEMORY_DIR = os.path.join(AGENT_ROOT, "memory")
CONTEXT_DIR = os.path.join(AGENT_ROOT, "context")

# LLM Configuration
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.minimaxi.chat/v1/text/chatcompletion_v2")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = "MiniMax-Text-01"


class Memory:
    """Simple file-based memory system"""
    
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        os.makedirs(root_dir, exist_ok=True)
        os.makedirs(os.path.join(root_dir, "daily"), exist_ok=True)
        os.makedirs(os.path.join(root_dir, "global"), exist_ok=True)
        os.makedirs(os.path.join(root_dir, "learnings"), exist_ok=True)
    
    def load_soul(self) -> str:
        """Load soul/core identity"""
        soul_path = os.path.join(AGENT_ROOT, "soul.md")
        if os.path.exists(soul_path):
            with open(soul_path, 'r') as f:
                return f.read()
        return ""
    
    def load_heartbeat(self) -> str:
        """Load heartbeat instructions"""
        hb_path = os.path.join(AGENT_ROOT, "heartbeat.md")
        if os.path.exists(hb_path):
            with open(hb_path, 'r') as f:
                return f.read()
        return ""
    
    def get_recent_daily(self, days: int = 7) -> List[Dict]:
        """Get recent daily memory files"""
        daily_dir = os.path.join(self.root_dir, "daily")
        files = []
        
        if os.path.exists(daily_dir):
            for f in os.listdir(daily_dir):
                if f.endswith('.md'):
                    files.append(os.path.join(daily_dir, f))
        
        # Sort by modification time, most recent first
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        
        result = []
        for path in files[:days]:
            with open(path, 'r') as f:
                result.append({
                    'file': os.path.basename(path),
                    'content': f.read()
                })
        
        return result
    
    def search_memory(self, query: str, max_results: int = 5) -> List[Dict]:
        """Search memory for relevant content"""
        results = []
        
        # Search daily files
        daily_dir = os.path.join(self.root_dir, "daily")
        if os.path.exists(daily_dir):
            for f in os.listdir(daily_dir):
                if f.endswith('.md'):
                    path = os.path.join(daily_dir, f)
                    with open(path, 'r') as file:
                        content = file.read()
                        if query.lower() in content.lower():
                            results.append({
                                'type': 'daily',
                                'file': f,
                                'content': content[:500],  # First 500 chars
                                'path': path
                            })
        
        # Search global files
        global_dir = os.path.join(self.root_dir, "global")
        if os.path.exists(global_dir):
            for f in os.listdir(global_dir):
                if f.endswith('.md'):
                    path = os.path.join(global_dir, f)
                    with open(path, 'r') as file:
                        content = file.read()
                        if query.lower() in content.lower():
                            results.append({
                                'type': 'global',
                                'file': f,
                                'content': content[:500],
                                'path': path
                            })
        
        return results[:max_results]
    
    def add_daily_note(self, note: str):
        """Add a note to today's daily file"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        daily_path = os.path.join(self.root_dir, "daily", f"{today}.md")
        
        timestamp = datetime.datetime.now().strftime("%H:%M")
        content = f"## {timestamp}\n\n{note}\n\n"
        
        if os.path.exists(daily_path):
            with open(daily_path, 'r') as f:
                content = f.read() + content
        
        with open(daily_path, 'w') as f:
            f.write(content)


class Agent:
    """Selena v2 Agent - Context -> Call -> Process Loop"""
    
    def __init__(self):
        self.memory = Memory(MEMORY_DIR)
        self.llm_calls = 0
        self.token_usage = 0
    
    def context(self, task: str, extra_context: str = "") -> str:
        """Gather context for the task"""
        parts = []
        
        # Load soul
        soul = self.memory.load_soul()
        if soul:
            parts.append(f"## SOUL (Core Identity)\n{soul}\n")
        
        # Load heartbeat
        heartbeat = self.memory.load_heartbeat()
        if heartbeat:
            parts.append(f"## HEARTBEAT INSTRUCTIONS\n{heartbeat}\n")
        
        # Get recent daily memories
        recent = self.memory.get_recent_daily(3)
        if recent:
            parts.append("## RECENT MEMORIES\n")
            for entry in recent:
                parts.append(f"### {entry['file']}\n{entry['content'][:300]}...\n")
        
        # Add task-specific context if provided
        if extra_context:
            parts.append(f"## ADDITIONAL CONTEXT\n{extra_context}\n")
        
        # Add the task itself
        parts.append(f"## TASK\n{task}\n")
        
        return "\n".join(parts)
    
    def call(self, prompt: str, system: str = "") -> Optional[str]:
        """Make LLM call"""
        if not LLM_API_KEY:
            return "⚠️ LLM API not configured"
        
        try:
            import requests
            
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            
            headers = {
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": LLM_MODEL,
                "messages": messages,
                "max_tokens": 1000,
                "temperature": 0.7
            }
            
            response = requests.post(
                LLM_API_URL,
                headers=headers,
                json=data,
                timeout=60
            )
            
            self.llm_calls += 1
            
            if response.status_code == 200:
                result = response.json()
                return result.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                return f"⚠️ LLM error: {response.status_code}"
        
        except Exception as e:
            return f"⚠️ LLM call failed: {e}"
    
    def process(self, response: str, task: str) -> Dict[str, Any]:
        """Process LLM response and determine follow-up"""
        # For now, just return the response
        return {
            "response": response,
            "needs_loop": False,
            "notes": []
        }
    
    def run(self, task: str, extra_context: str = "") -> str:
        """Run the context -> call -> process loop"""
        # Step 1: Gather context
        print("📋 Gathering context...")
        ctx = self.context(task, extra_context)
        
        # Step 2: Make LLM call
        print("🤖 Making LLM call...")
        response = self.call(ctx)
        
        if not response:
            return "⚠️ No response from LLM"
        
        # Step 3: Process response
        print("⚙️ Processing response...")
        result = self.process(response, task)
        
        # Step 4: Add notes to memory if needed
        if result.get("notes"):
            for note in result["notes"]:
                self.memory.add_daily_note(note)
        
        # Step 5: Loop if needed
        if result.get("needs_loop"):
            print("🔄 Looping...")
            # Recursive call with updated context
            return self.run(task, response)
        
        print(f"✅ Completed in {self.llm_calls} LLM call(s)")
        return result["response"]


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python3 agent_loop.py <task>")
        print("Example: python3 agent_loop.py \"What should I work on next?\"")
        sys.exit(1)
    
    task = " ".join(sys.argv[1:])
    
    print(f"🎯 Starting agent with task: {task}")
    print()
    
    agent = Agent()
    response = agent.run(task)
    
    print()
    print("=" * 50)
    print("RESPONSE:")
    print("=" * 50)
    print(response)


if __name__ == "__main__":
    main()
