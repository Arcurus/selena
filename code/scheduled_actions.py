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
OPEN_WORLD_URL = "http://localhost:8081"
MAX_TOKENS_PER_CALL = 500  # Limit per action

# Auth cookie - set after password verification
AUTH_COOKIE = "openworld_auth=1"

# Scheduler behaviour is now read live from a JSON config so we can tune
# it (interval, actions per cycle, on/off) without restarting the API
# server.  Default values are picked to stay well under the OW server's
# 5% MiniMax slice (45 LLM calls / hour):
#   1 action every 120s  =>  30 calls/hour
# leaving ~15 calls/h of headroom for ad-hoc LLM calls in the OW web UI.
SCHEDULER_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'ow_scheduler_config.json'
)
SCHEDULER_CONFIG_DEFAULTS = {
    "enabled": True,
    "interval_seconds": 120,   # 2 minutes between cycles
    "actions_per_cycle": 1,    # 1 entity per cycle
    "notes": "Defaults: 30 LLM calls/h, under 5% of 4500/5h budget.",
}


def load_scheduler_config() -> dict:
    """Load scheduler config from disk; create it with defaults if missing.

    Reads live every call so runtime changes via the API take effect on
    the next cycle without restarting the process.
    """
    try:
        if os.path.exists(SCHEDULER_CONFIG_PATH):
            with open(SCHEDULER_CONFIG_PATH, "r") as f:
                disk = json.load(f)
            # Merge with defaults so missing keys fall back safely.
            merged = {**SCHEDULER_CONFIG_DEFAULTS, **disk}
            # Coerce types defensively (file may have been hand-edited).
            merged["enabled"] = bool(merged.get("enabled", True))
            merged["interval_seconds"] = max(5, int(merged.get("interval_seconds", 120)))
            merged["actions_per_cycle"] = max(0, int(merged.get("actions_per_cycle", 1)))
            return merged
    except Exception as e:
        print(f"[scheduler] config read failed ({e}); using defaults")
    # First run / read failure: write defaults so the file exists.
    try:
        os.makedirs(os.path.dirname(SCHEDULER_CONFIG_PATH), exist_ok=True)
        with open(SCHEDULER_CONFIG_PATH, "w") as f:
            json.dump(SCHEDULER_CONFIG_DEFAULTS, f, indent=2)
    except Exception:
        pass
    return dict(SCHEDULER_CONFIG_DEFAULTS)


def save_scheduler_config(cfg: dict, updated_by: str = "selena") -> dict:
    """Persist scheduler config to disk. Returns the merged config.

    Validates the inputs, clamps to safe ranges, and stamps updated_at /
    updated_by so we can audit changes.
    """
    merged = {**SCHEDULER_CONFIG_DEFAULTS, **(cfg or {})}
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["interval_seconds"] = max(5, min(3600, int(merged.get("interval_seconds", 120))))
    merged["actions_per_cycle"] = max(0, min(20, int(merged.get("actions_per_cycle", 1))))
    merged["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    merged["updated_by"] = updated_by
    try:
        os.makedirs(os.path.dirname(SCHEDULER_CONFIG_PATH), exist_ok=True)
        with open(SCHEDULER_CONFIG_PATH, "w") as f:
            json.dump(merged, f, indent=2)
    except Exception as e:
        print(f"[scheduler] config write failed: {e}")
    return merged


# Backwards-compat constants — used only by tests / legacy callers that
# still import these names. The runtime scheduler reads from the config.
SCHEDULE_INTERVAL_SECONDS = SCHEDULER_CONFIG_DEFAULTS["interval_seconds"]
ACTIONS_PER_CYCLE = SCHEDULER_CONFIG_DEFAULTS["actions_per_cycle"]


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
        self.last_config_signature = None  # detect live config changes

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
        cfg = load_scheduler_config()
        if not cfg.get("enabled", True):
            self.log_msg("=== Scheduler cycle skipped: disabled in config ===")
            return
        actions_per_cycle = int(cfg.get("actions_per_cycle", 1))
        self.log_msg(f"=== Running scheduled world action cycle ({actions_per_cycle} actions) ===")
        
        # Get all entities
        entities = self.get_entities()
        if not entities:
            self.log_msg("No entities found in world!")
            return
        
        # Run multiple actions per cycle
        for i in range(actions_per_cycle):
            if not self.running:
                break
            
            # Pick a random entity
            entity = random.choice(entities)
            entity_id = entity["id"]
            entity_name = entity["name"]
            entity_type = entity.get("entity_type", "unknown")
            
            self.log_msg(f"  [{i+1}/{actions_per_cycle}] Entity: {entity_name} ({entity_type})")
            
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
        """Main scheduler loop - runs actions on schedule.

        Reads the scheduler config live on every iteration, so changes
        made via the API (interval / actions_per_cycle / enabled) take
        effect on the next cycle without a restart.
        """
        cfg = load_scheduler_config()
        self.log_msg(
            f"World Scheduler started! Interval: {cfg['interval_seconds']}s, "
            f"{cfg['actions_per_cycle']} actions/cycle, enabled={cfg['enabled']}"
        )
        self.log_msg(f"Open World URL: {OPEN_WORLD_URL}")
        self.last_config_signature = (
            cfg['enabled'], cfg['interval_seconds'], cfg['actions_per_cycle']
        )

        # Run once immediately on start (respects the enabled flag)
        self.execute_scheduled_action()

        while self.running:
            try:
                cfg = load_scheduler_config()
                signature = (
                    cfg['enabled'], cfg['interval_seconds'], cfg['actions_per_cycle']
                )
                if signature != self.last_config_signature:
                    self.log_msg(
                        f"[scheduler] config changed: interval={cfg['interval_seconds']}s, "
                        f"actions/cycle={cfg['actions_per_cycle']}, enabled={cfg['enabled']}"
                    )
                    self.last_config_signature = signature
                if not cfg['enabled']:
                    # When disabled, wake up every 30s just to check the flag.
                    time.sleep(30)
                    continue
                # Wait for next interval (or until we wake to check config).
                slept = 0
                interval = cfg['interval_seconds']
                while slept < interval and self.running:
                    step = min(5, interval - slept)
                    time.sleep(step)
                    slept += step
                if self.running:
                    self.execute_scheduled_action()
            except Exception as e:
                self.log_msg(f"Scheduler error (will continue): {e}")
                # Continue running even if an action fails
                time.sleep(10)

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
        cfg = load_scheduler_config()
        interval = max(1, int(cfg.get("interval_seconds", 120)))
        actions = int(cfg.get("actions_per_cycle", 1))
        # Derive the projected LLM-call rate (calls/h) for budget awareness.
        projected_calls_per_hour = round(actions * 3600.0 / interval, 2)
        return {
            "running": self.running,
            "enabled": cfg.get("enabled", True),
            "interval_seconds": interval,
            "actions_per_cycle": actions,
            "projected_calls_per_hour": projected_calls_per_hour,
            "config": cfg,
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
