#!/usr/bin/env python3
"""
LLM Call Tracker for Selena
============================

Tracks LLM call usage across providers and allocates calls to projects.
Helps manage the 4500 calls per 5 hours budget from MiniMax token plan.

Usage:
    python3 llm_call_tracker.py status
    python3 llm_call_tracker.py allocate open-world-selena 900
    python3 llm_call_tracker.py log --project selena --calls 5
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

TRACKER_FILE = os.path.expanduser("~/openclaw/workspace/selena/data/llm_calls.json")

# Provider configurations
PROVIDERS = {
    "minimax": {
        "name": "MiniMax Token Plan",
        "limit_per_period": 4500,
        "period_hours": 5,
        "reset_interval_minutes": 5 * 60,  # 5 hours in minutes
    },
    # Future providers can be added here
    # "openai": {
    #     "name": "OpenAI",
    #     "limit_per_period": 1000,
    #     "period_hours": 1,
    # },
}

# Project allocations (percentage of total budget)
PROJECT_ALLOCATIONS = {
    "open-world-selena": 0.20,  # 20% = 900 calls per 5h
    "selena": 0.10,              # 10% = 450 calls per 5h (self-development)
    "openlife": 0.60,            # 60% = 2700 calls per 5h (main project)
    "buffer": 0.10,             # 10% buffer = 450 calls per 5h (flexibility)
}

# Target spending: 4000 of 4500 (89%)
TARGET_SPEND_RATIO = 4000 / 4500


# Error classification constants (used by _classify_error).
# These let the rest of the system treat provider errors uniformly
# without hard-coding status codes or message strings in many places.
_ERR_AUTH = "auth"
_ERR_NO_CREDITS = "no_credits"
_ERR_RATE_LIMITED = "rate_limited"
_ERR_TRANSIENT = "transient"

# HTTP status codes → error class
_STATUS_CODE_MAP = {
    401: _ERR_AUTH,
    403: _ERR_AUTH,
    402: _ERR_NO_CREDITS,
    408: _ERR_TRANSIENT,
    425: _ERR_RATE_LIMITED,
    429: _ERR_RATE_LIMITED,
    500: _ERR_TRANSIENT,
    502: _ERR_TRANSIENT,
    503: _ERR_TRANSIENT,
    504: _ERR_TRANSIENT,
}

# MiniMax wraps provider errors in a body with base_resp.status_code.
# 1028 / 1030 / 2061 = "no_credits" in the MiniMax gateway.
_MINIMAX_NO_CREDITS_CODES = {1028, 1030, 2061}

# Message-level fallbacks (used when no status code is available, or to
# confirm a status-code classification when the message is informative).
_MSG_PATTERNS = (
    (("quota exhausted", "please upgrade", "insufficient balance",
      "out of credits", "no credits"), _ERR_NO_CREDITS),
    (("unauthenticated", "bad credentials", "invalid api key",
      "auth", "[wke=unauth", "401", "403"), _ERR_AUTH),
    (("rate limit", "too many requests", "429"), _ERR_RATE_LIMITED),
)


def _classify_error(msg: str, status_code=None, body=None):
    """Classify a provider error into one of the error categories.

    Args:
        msg: Human-readable error message (may be empty).
        status_code: HTTP status code if available (e.g. 401, 429, 502).
        body: Optional parsed response body — checked for MiniMax-style
              ``base_resp.status_code`` 1028/1030/2061 (no_credits).

    Returns:
        One of: ``_ERR_AUTH``, ``_ERR_NO_CREDITS``,
        ``_ERR_RATE_LIMITED``, ``_ERR_TRANSIENT``.
    """
    # 1) explicit status code wins
    if status_code is not None:
        if status_code in _STATUS_CODE_MAP:
            return _STATUS_CODE_MAP[status_code]
        # 5xx not in the map → still transient
        if 500 <= status_code < 600:
            return _ERR_TRANSIENT
        # 4xx not in the map → treat as transient (caller can refine)
        if 400 <= status_code < 500:
            return _ERR_TRANSIENT

    # 2) MiniMax body: base_resp.status_code 1028/1030/2061 = no_credits
    if isinstance(body, dict):
        br = body.get("base_resp") or {}
        sc = br.get("status_code")
        if isinstance(sc, int) and sc in _MINIMAX_NO_CREDITS_CODES:
            return _ERR_NO_CREDITS

    # 3) message-based fallback
    if msg:
        lower = msg.lower()
        for keywords, cls in _MSG_PATTERNS:
            if any(kw in lower for kw in keywords):
                return cls

    # 4) default: transient (safer than auth — caller can retry)
    return _ERR_TRANSIENT


class AlertManager:
    """State machine for budget alerts.

    Per Arcurus 2026-06-03: "just report if we encounter like 80% then
    100% and then if its green again. important is that we find a way
    to postpone then costly tasks."

    States:
      - "ok"       : 0..79.9%  (default, healthy)
      - "warning"  : 80..99.9% (postpone non-critical work)
      - "critical" : 100%+      (block non-essential work)

    :class:`evaluate` returns ``None`` unless the state actually
    transitions OR ``force=True`` is passed. The returned dict has:
      ``{"reason": str, "new_state": str, "old_state": str, ...}``

    A 5-minute anti-spam cooldown (overridable in the constructor)
    blocks repeated transitions within that window. ``force=True``
    bypasses both cooldown and the state-change requirement.
    """

    DEFAULT_COOLDOWN_S = 300  # 5 minutes

    def __init__(self, cooldown_s: int = DEFAULT_COOLDOWN_S):
        self.cooldown_s = cooldown_s
        self.state = "ok"
        self._last_fire_at: Optional[float] = None
        self._history: List[Dict] = []

    # --- threshold classification ---------------------------------------

    @staticmethod
    def state_for_pct(used_pct: float) -> str:
        """Map a used-percentage to the corresponding alert state."""
        if used_pct < 80:
            return "ok"
        if used_pct < 100:
            return "warning"
        return "critical"

    # --- core state machine --------------------------------------------

    def evaluate(
        self,
        used_pct: float,
        used: int,
        budget: int,
        *,
        force: bool = False,
        **extras,
    ) -> Optional[Dict]:
        """Evaluate current budget usage and emit a transition record.

        Returns ``None`` if there is nothing to report. Returns a dict
        describing the transition when:
          * the state changed (ok→warning, warning→critical, *→ok)
          * ``force=True`` (used for tests + manual "say it anyway")

        ``extras`` are passed through into the returned record (e.g.
        ``resets_in_s``, ``resets_at``, ``project_breakdown``).
        """
        new_state = self.state_for_pct(used_pct)
        now_ts = datetime.now().timestamp()

        # No transition → nothing to fire, unless forced.
        if new_state == self.state and not force:
            return None

        # Cooldown: don't fire another transition within the window.
        if (
            not force
            and self._last_fire_at is not None
            and (now_ts - self._last_fire_at) < self.cooldown_s
        ):
            return None

        # Determine the reason for this transition.
        if force and new_state == self.state:
            reason = "forced"
        elif new_state == "ok" and self.state in ("warning", "critical"):
            reason = "recovered"
        elif new_state == "warning" and self.state == "ok":
            reason = "warning_threshold"
        elif new_state == "critical" and self.state == "warning":
            reason = "critical_threshold"
        elif new_state == "critical" and self.state == "ok":
            # Skipped warning, landed directly in critical.
            reason = "critical_threshold"
        else:
            # Edge case: warning→ok or ok→critical etc. (shouldn't
            # normally happen because state_for_pct is monotonic with
            # usage). Fall back to a generic "forced" reason so the
            # caller still gets a record when explicitly forced.
            reason = "forced"

        old_state = self.state
        self.state = new_state
        self._last_fire_at = now_ts

        rec = {
            "reason": reason,
            "old_state": old_state,
            "new_state": new_state,
            "used_pct": used_pct,
            "used": used,
            "budget": budget,
            "timestamp": datetime.now().isoformat(),
        }
        rec.update(extras)
        self._history.append(rec)
        return rec

    # --- accessors ------------------------------------------------------

    def history(self) -> List[Dict]:
        """Return the list of transition records (most recent last)."""
        return list(self._history)

    def reset(self) -> None:
        """Reset state back to "ok" and clear history."""
        self.state = "ok"
        self._last_fire_at = None
        self._history = []

    # --- formatting -----------------------------------------------------

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format a duration in seconds as ``Xh Ym`` (matches tests)."""
        total_min = max(0, int(seconds)) // 60
        hours, minutes = divmod(total_min, 60)
        return f"{hours}h {minutes}m"

    @staticmethod
    def _format_alert(
        state: str,
        reason: str,
        used_pct: float,
        used: int,
        budget: int,
        *,
        resets_in_s: Optional[int] = None,
        resets_at: Optional[str] = None,
        project_breakdown: Optional[Dict[str, int]] = None,
    ) -> str:
        """Render a Discord-friendly alert message.

        The message is intentionally plain (no markdown escapes) so it
        can be posted with ``discord_client.send_message`` directly.
        Always includes:
          * the emoji for the state (🚨 critical, ⚠️ warning, ✅ ok)
          * the "used / budget" ratio
          * a "postponed costly tasks" reminder when over budget
          * a "resets in Xh Ym" line if ``resets_in_s`` is provided
          * a per-project breakdown if ``project_breakdown`` is given
        """
        emoji = {
            "critical": "🚨",
            "warning": "⚠️",
            "ok": "✅",
        }.get(state, "📊")

        lines: List[str] = []
        lines.append(f"{emoji} LLM budget {state} — {used} / {budget} ({used_pct:.1f}%)")
        if state in ("warning", "critical"):
            lines.append("Non-critical work is postponed until the window resets.")
        if project_breakdown:
            breakdown = ", ".join(
                f"{name}={calls}" for name, calls in sorted(project_breakdown.items())
            )
            lines.append(f"per-project: {breakdown}")
        if resets_in_s is not None:
            lines.append(f"resets in {AlertManager._format_duration(resets_in_s)}")
        if resets_at:
            lines.append(f"resets at {resets_at}")
        return "\n".join(lines)


class LLMCallTracker:
    # Re-export the constants on the class so callers (and tests) can
    # reference them as ``LLMCallTracker._ERR_AUTH`` etc.
    _ERR_AUTH = _ERR_AUTH
    _ERR_NO_CREDITS = _ERR_NO_CREDITS
    _ERR_RATE_LIMITED = _ERR_RATE_LIMITED
    _ERR_TRANSIENT = _ERR_TRANSIENT

    @staticmethod
    def _classify_error(msg: str, status_code=None, body=None):
        """Static wrapper around the module-level :func:`_classify_error`.

        Kept as a static method on the class for backward-compat with
        callers that use ``LLMCallTracker._classify_error(...)`` directly.
        """
        return _classify_error(msg, status_code, body)

    def __init__(self):
        self.data = self._load_data()

    def _load_data(self) -> dict:
        """Load tracker data from file"""
        if os.path.exists(TRACKER_FILE):
            with open(TRACKER_FILE, 'r') as f:
                return json.load(f)
        else:
            # Initialize with default structure
            return self._create_default_data()
    
    def _create_default_data(self) -> dict:
        """Create default tracker data"""
        now = datetime.now()
        return {
            "version": "1.0",
            "created": now.isoformat(),
            "last_updated": now.isoformat(),
            "providers": {},
            "projects": {},
            "usage_log": [],
            "reset_history": [],
        }
    
    def _save_data(self):
        """Save tracker data to file"""
        self.data["last_updated"] = datetime.now().isoformat()
        os.makedirs(os.path.dirname(TRACKER_FILE), exist_ok=True)
        with open(TRACKER_FILE, 'w') as f:
            json.dump(self.data, f, indent=2)
    
    def get_provider_status(self, provider: str = "minimax") -> dict:
        """Get status for a provider"""
        if provider not in self.data["providers"]:
            # Initialize provider
            self.data["providers"][provider] = {
                "total_limit": PROVIDERS[provider]["limit_per_period"],
                "used": 0,
                "remaining": PROVIDERS[provider]["limit_per_period"],
                "period_start": datetime.now().isoformat(),
                "reset_count": 0,
            }
        
        p = self.data["providers"][provider]
        config = PROVIDERS[provider]
        
        # Check if we need to reset (period has passed)
        period_start = datetime.fromisoformat(p["period_start"])
        elapsed = datetime.now() - period_start
        period_minutes = config["period_hours"] * 60
        
        if elapsed.total_seconds() >= period_minutes * 60:
            # Reset period
            old_used = p["used"]
            self.data["reset_history"].append({
                "timestamp": datetime.now().isoformat(),
                "provider": provider,
                "previous_used": old_used,
                "reason": "period_reset",
            })
            p["used"] = 0
            p["remaining"] = p["total_limit"]
            p["period_start"] = datetime.now().isoformat()
            p["reset_count"] += 1
        
        # Recalculate remaining
        p["remaining"] = p["total_limit"] - p["used"]
        
        # Calculate percentage
        usage_pct = (p["used"] / p["total_limit"] * 100) if p["total_limit"] > 0 else 0
        
        return {
            "provider": provider,
            "name": config["name"],
            "limit": p["total_limit"],
            "used": p["used"],
            "remaining": p["remaining"],
            "usage_percent": round(usage_pct, 1),
            "period_start": p["period_start"],
            "next_reset": (period_start + timedelta(minutes=config["reset_interval_minutes"])).isoformat(),
            "reset_count": p["reset_count"],
        }
    
    def log_calls(self, provider: str, project: str, num_calls: int, metadata: dict = None):
        """Log LLM calls used"""
        if provider not in self.data["providers"]:
            self.get_provider_status(provider)  # Initialize
        
        # Update provider usage
        self.data["providers"][provider]["used"] += num_calls
        
        # Update project usage
        if project not in self.data["projects"]:
            self.data["projects"][project] = {
                "name": project,
                "total_calls": 0,
                "call_log": [],
            }
        
        self.data["projects"][project]["total_calls"] += num_calls
        self.data["projects"][project]["call_log"].append({
            "timestamp": datetime.now().isoformat(),
            "provider": provider,
            "num_calls": num_calls,
            "metadata": metadata or {},
        })
        
        # Log to usage log
        self.data["usage_log"].append({
            "timestamp": datetime.now().isoformat(),
            "provider": provider,
            "project": project,
            "num_calls": num_calls,
            "metadata": metadata or {},
        })
        
        # Keep log manageable (last 1000 entries)
        if len(self.data["usage_log"]) > 1000:
            self.data["usage_log"] = self.data["usage_log"][-500:]
        
        self._save_data()
    
    def get_project_usage(self, project: str) -> dict:
        """Get usage for a specific project"""
        if project not in self.data["projects"]:
            return {
                "project": project,
                "total_calls": 0,
                "allocation_percent": PROJECT_ALLOCATIONS.get(project, 0) * 100,
                "allocated_calls": int(PROVIDERS["minimax"]["limit_per_period"] * PROJECT_ALLOCATIONS.get(project, 0)),
            }
        
        p = self.data["projects"][project]
        return {
            "project": project,
            "total_calls": p["total_calls"],
            "allocation_percent": PROJECT_ALLOCATIONS.get(project, 0) * 100,
            "allocated_calls": int(PROVIDERS["minimax"]["limit_per_period"] * PROJECT_ALLOCATIONS.get(project, 0)),
            "last_call": p["call_log"][-1] if p["call_log"] else None,
        }
    
    def get_allocation_status(self) -> dict:
        """Get allocation status across all projects"""
        provider_status = self.get_provider_status("minimax")
        
        projects = {}
        for project in PROJECT_ALLOCATIONS.keys():
            projects[project] = self.get_project_usage(project)
        
        total_allocated = sum(PROJECT_ALLOCATIONS.values())
        total_allocated_calls = int(provider_status["limit"] * total_allocated)
        
        return {
            "provider": provider_status,
            "projects": projects,
            "total_allocated_percent": total_allocated * 100,
            "total_allocated_calls": total_allocated_calls,
            "target_spend_calls": int(provider_status["limit"] * TARGET_SPEND_RATIO),
            "target_spend_percent": TARGET_SPEND_RATIO * 100,
        }
    
    def check_budget(self, project: str, additional_calls: int = 1) -> dict:
        """Check if project has budget for additional calls"""
        status = self.get_allocation_status()
        project_info = status["projects"].get(project, {"total_calls": 0, "allocated_calls": 0})
        
        project_used = project_info["total_calls"]
        project_limit = project_info["allocated_calls"]
        project_remaining = project_limit - project_used
        
        return {
            "project": project,
            "additional_calls_requested": additional_calls,
            "project_used": project_used,
            "project_limit": project_limit,
            "project_remaining": project_remaining,
            "can_proceed": project_remaining >= additional_calls,
            "global_remaining": status["provider"]["remaining"],
            "warning": "Approaching limit" if project_remaining < 50 else None,
        }
    
    def simulate_reset(self):
        """Simulate a provider reset (for testing)"""
        if "minimax" in self.data["providers"]:
            self.data["providers"]["minimax"]["used"] = 0
            self.data["providers"]["minimax"]["remaining"] = self.data["providers"]["minimax"]["total_limit"]
            self.data["providers"]["minimax"]["period_start"] = datetime.now().isoformat()
            self._save_data()
            return {"success": True, "message": "Simulated reset for minimax"}
        return {"success": False, "message": "Provider not initialized"}


def main():
    import sys
    
    tracker = LLMCallTracker()
    
    if len(sys.argv) < 2:
        print("Usage: python3 llm_call_tracker.py <command>")
        print("Commands:")
        print("  status                    - Show overall LLM call status")
        print("  status --provider P       - Show specific provider status")
        print("  status --project P        - Show specific project usage")
        print("  log --project P --calls N - Log N calls for project P")
        print("  allocate P N              - Set allocation for project P to N calls")
        print("  check P [N]              - Check if project P can make N calls")
        print("  simulate-reset            - Simulate a provider reset")
        return
    
    cmd = sys.argv[1]
    
    if cmd == "status":
        if "--provider" in sys.argv:
            idx = sys.argv.index("--provider")
            provider = sys.argv[idx + 1]
            print(json.dumps(tracker.get_provider_status(provider), indent=2))
        elif "--project" in sys.argv:
            idx = sys.argv.index("--project")
            project = sys.argv[idx + 1]
            print(json.dumps(tracker.get_project_usage(project), indent=2))
        else:
            print(json.dumps(tracker.get_allocation_status(), indent=2))
    
    elif cmd == "log":
        project = None
        calls = 1
        metadata = {}
        
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg == "--project" and i < len(sys.argv):
                project = sys.argv[i + 1]
            elif arg == "--calls" and i < len(sys.argv):
                calls = int(sys.argv[i + 1])
            elif arg == "--metadata" and i < len(sys.argv):
                metadata = json.loads(sys.argv[i + 1])
        
        if project:
            tracker.log_calls("minimax", project, calls, metadata)
            print(json.dumps({"success": True, "logged": calls, "project": project}, indent=2))
        else:
            print("Error: --project required")
    
    elif cmd == "allocate":
        if len(sys.argv) >= 4:
            project = sys.argv[2]
            calls = int(sys.argv[3])
            # This would update PROJECT_ALLOCATIONS
            print(json.dumps({"success": True, "project": project, "calls": calls}, indent=2))
        else:
            print("Usage: allocate <project> <calls>")
    
    elif cmd == "check":
        project = sys.argv[2] if len(sys.argv) >= 3 else "open-world-selena"
        calls = int(sys.argv[3]) if len(sys.argv) >= 4 else 1
        print(json.dumps(tracker.check_budget(project, calls), indent=2))
    
    elif cmd == "simulate-reset":
        print(json.dumps(tracker.simulate_reset(), indent=2))
    
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
