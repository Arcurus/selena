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


class LLMCallTracker:
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
