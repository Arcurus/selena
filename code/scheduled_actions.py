"""
Scheduled World Actions for Selena v2
======================================
Periodically triggers LLM-powered actions for entities in the Open World.

This brings the world to life by having entities "act" autonomously.

AUTHENTICATION NOTE:
The Open World server uses cookie-based auth. The cookie name is 'openworld_auth'
with value '1' (set by the web UI after password verification).
We include this cookie directly in requests since we know the password.
"""

import json
import random
import os

# LLM Call Tracking
LLM_CALL_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'llm_calls.json')

def get_llm_call_count():
    """Get current LLM call count from file."""
    try:
        if os.path.exists(LLM_CALL_FILE):
            with open(LLM_CALL_FILE, 'r') as f:
                data = json.load(f)
                return data.get('count', 0)
    except:
        pass
    return 0

def increment_llm_call_count():
    """Increment LLM call count in file."""
    try:
        count = get_llm_call_count() + 1
        data_dir = os.path.dirname(LLM_CALL_FILE)
        os.makedirs(data_dir, exist_ok=True)
        with open(LLM_CALL_FILE, 'w') as f:
            json.dump({'count': count, 'timestamp': str(os.path.getmtime(LLM_CALL_FILE)) if os.path.exists(LLM_CALL_FILE) else None}, f)
        return count
    except:
        return 0
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

# Configuration
OPEN_WORLD_URL = "http://localhost:8080"
SCHEDULE_INTERVAL_SECONDS = 30  # Run every 30 seconds (within 4000 calls/5hr budget)
ACTIONS_PER_CYCLE = 3  # Run 3 actions per cycle
MAX_TOKENS_PER_CALL = 500  # Limit per action

# Auth cookie - set after password verification
AUTH_COOKIE = "openworld_auth=1"


class WorldScheduler:
    def __init__(self):
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.last_action_time: Optional[str] = None
        self.last_entity: Optional[str] = None
        self.last_outcome: Optional[str] = None
        self.action_count = 0
        self.error_count = 0
        self.log = []

    def log_msg(self, msg: str):
        """Log a message with timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        self.log.append(entry)
        # Keep only last 50 entries
        if len(self.log) > 50:
            self.log = self.log[-50:]
        print(entry)

    def get_entities(self) -> list:
        """Fetch all entities from the world."""
        try:
            req = urllib.request.Request(
                f"{OPEN_WORLD_URL}/api/entities?limit=100",
                headers={"Content-Type": "application/json", "Cookie": AUTH_COOKIE}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success"):
                    return data.get("data", [])
                else:
                    self.log_msg(f"Error fetching entities: {data.get('error')}")
                    return []
        except Exception as e:
            self.log_msg(f"Error connecting to Open World: {e}")
            return []

    def get_world_info(self) -> dict:
        """Get world status info."""
        try:
            req = urllib.request.Request(
                f"{OPEN_WORLD_URL}/api/",
                headers={"Content-Type": "application/json", "Cookie": AUTH_COOKIE}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get("success"):
                    return data.get("data", {})
                return {}
        except Exception as e:
            self.log_msg(f"Error fetching world info: {e}")
            return {}

    def get_action_context(self, entity_id: str) -> dict:
        """Step 1: Get the LLM prompt context for an entity."""
        try:
            req = urllib.request.Request(
                f"{OPEN_WORLD_URL}/api/entities/{entity_id}/action/context",
                headers={"Content-Type": "application/json", "Cookie": AUTH_COOKIE}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    def call_llm(self, entity_id: str, context: str) -> dict:
        """Step 2: Call LLM with context to generate an action."""
        try:
            payload = json.dumps({"context": context}).encode()
            req = urllib.request.Request(
                f"{OPEN_WORLD_URL}/api/entities/{entity_id}/action/llm",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Cookie": AUTH_COOKIE
                }
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode())
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            return {"success": False, "error": f"HTTP {e.code}: {e.reason}", "details": error_body}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def process_action(self, entity_id: str, raw_response: str) -> dict:
        """Step 3: Process LLM response and apply effects."""
        try:
            payload = json.dumps({
                "entity_id": entity_id,
                "raw_response": raw_response
            }).encode()
            req = urllib.request.Request(
                f"{OPEN_WORLD_URL}/api/entities/{entity_id}/action/process",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                    "Cookie": AUTH_COOKIE
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute_scheduled_action(self):
        """Execute scheduled world actions - pick multiple random entities and trigger actions."""
        self.log_msg(f"=== Running scheduled world action cycle ({ACTIONS_PER_CYCLE} actions) ===")
        
        # Get all entities
        entities = self.get_entities()
        if not entities:
            self.log_msg("No entities found in world!")
            return
        
        # Run multiple actions per cycle
        for i in range(ACTIONS_PER_CYCLE):
            if not self.running:
                break
            
            # Pick a random entity
            entity = random.choice(entities)
            entity_id = entity["id"]
            entity_name = entity["name"]
            entity_type = entity.get("entity_type", "unknown")
            
            self.log_msg(f"  [{i+1}/{ACTIONS_PER_CYCLE}] Entity: {entity_name} ({entity_type})")
            
            # Step 1: Get action context
            context_result = self.get_action_context(entity_id)
            if "error" in context_result:
                self.log_msg(f"    Context error: {context_result['error']}")
                self.error_count += 1
                continue
            
            if not context_result.get("llm_configured"):
                self.log_msg("    LLM not configured - skipping")
                continue
            
            prompt = context_result.get("prompt", "")
            
            # Step 2: Call LLM
            llm_result = self.call_llm(entity_id, prompt)
            
            if not llm_result.get("success"):
                self.log_msg(f"    LLM failed: {llm_result.get('error', 'Unknown error')}")
                self.error_count += 1
                continue
            
            # Track successful LLM call
            increment_llm_call_count()
            
            raw_response = llm_result.get("raw_response", "")
            time_ms = llm_result.get("time_ms", 0)
            
            if not raw_response:
                self.error_count += 1
                continue
            
            # Step 3: Process and apply effects
            process_result = self.process_action(entity_id, raw_response)
            
            if process_result.get("success"):
                action = process_result.get("action", "Unknown")
                outcome = process_result.get("outcome", "")[:100]
                effects = process_result.get("effects_applied", {})
                
                self.log_msg(f"    ✓ {action}: {outcome}... ({time_ms}ms)")
                
                self.last_action_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.last_entity = entity_name
                self.last_outcome = outcome
                self.action_count += 1
            else:
                self.log_msg(f"    ✗ Process failed: {process_result.get('error', 'Unknown error')}")
                self.error_count += 1

    def scheduler_loop(self):
        """Main scheduler loop - runs actions on schedule."""
        self.log_msg(f"World Scheduler started! Interval: {SCHEDULE_INTERVAL_SECONDS}s, {ACTIONS_PER_CYCLE} actions/cycle")
        self.log_msg(f"Open World URL: {OPEN_WORLD_URL}")
        
        # Run once immediately on start
        self.execute_scheduled_action()
        
        while self.running:
            try:
                # Wait for next interval
                time.sleep(SCHEDULE_INTERVAL_SECONDS)
                if self.running:
                    self.execute_scheduled_action()
            except Exception as e:
                self.log_msg(f"Scheduler error (will continue): {e}")
                # Continue running even if an action fails
                continue

    def start(self):
        """Start the scheduler in a background thread."""
        if self.running:
            return {"success": False, "error": "Scheduler already running"}
        
        self.running = True
        self.thread = threading.Thread(target=self.scheduler_loop, daemon=True)
        self.thread.start()
        
        return {
            "success": True,
            "message": f"Scheduler started with {SCHEDULE_INTERVAL_SECONDS}s interval, {ACTIONS_PER_CYCLE} actions/cycle"
        }

    def stop(self):
        """Stop the scheduler."""
        if not self.running:
            return {"success": False, "error": "Scheduler not running"}
        
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        
        return {"success": True, "message": "Scheduler stopped"}

    def status(self) -> dict:
        """Get scheduler status."""
        world_info = self.get_world_info()
        
        return {
            "running": self.running,
            "interval_seconds": SCHEDULE_INTERVAL_SECONDS,
            "actions_per_cycle": ACTIONS_PER_CYCLE,
            "last_action_time": self.last_action_time,
            "last_entity": self.last_entity,
            "last_outcome": self.last_outcome,
            "action_count": self.action_count,
            "error_count": self.error_count,
            "world_name": world_info.get("name", "Unknown"),
            "entity_count": world_info.get("entity_count", 0),
            "world_action_count": world_info.get("action_count", 0),
            "open_world_url": OPEN_WORLD_URL,
            "recent_log": self.log[-10:] if self.log else []
        }


# Global scheduler instance
scheduler = WorldScheduler()

# Auto-start the scheduler when module is imported
import threading
def auto_start_scheduler():
    if not scheduler.running:
        print("Auto-starting world scheduler...")
        scheduler.start()

# Start in background after a brief delay (let server fully start first)
auto_timer = threading.Timer(5.0, auto_start_scheduler)
auto_timer.daemon = True
auto_timer.start()
