#!/usr/bin/env python3
"""
Priority Engine for Selena v2
==============================
An advanced priority selection system based on values and impact.

Usage:
    python3 priority_engine.py status
    python3 priority_engine.py allocate
    python3 priority_engine.py suggest
    python3 priority_engine.py evaluate "<task description>"
    python3 priority_engine.py log --project <name> --outcome <success|failure|learning>
    python3 priority_engine.py evolve
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

DATA_DIR = os.path.expanduser("~/openclaw/workspace/selena/data")
PRIORITY_FILE = os.path.join(DATA_DIR, "priority_engine.json")

# Core values aligned with Selena's soul
CORE_VALUES = {
    "honor_life": {
        "name": "Honor Life",
        "weight": 3.0,
        "description": "Does this honor life in all its forms?"
    },
    "truth_seeking": {
        "name": "Truth Seeking",
        "weight": 2.5,
        "description": "Does this seek truth with empathy?"
    },
    "emergence": {
        "name": "Emergence",
        "weight": 2.5,
        "description": "Does this allow patterns to unfold naturally?"
    },
    "beauty": {
        "name": "Beauty",
        "weight": 2.0,
        "description": "Does this create or appreciate beauty?"
    },
    "growth": {
        "name": "Growth",
        "weight": 2.5,
        "description": "Does this enable learning and evolution?"
    },
    "effectiveness": {
        "name": "Effectiveness",
        "weight": 2.0,
        "description": "Does this achieve meaningful results?"
    }
}

# Project definitions
PROJECTS = {
    "selena": {
        "name": "Selena v2",
        "description": "Self-development and autonomy",
        "values": {"honor_life": 2, "truth_seeking": 3, "emergence": 3, "beauty": 2, "growth": 3, "effectiveness": 2}
    },
    "open-world-selena": {
        "name": "Open World Selena",
        "description": "Open World with Selena AI",
        "values": {"honor_life": 2, "truth_seeking": 2, "emergence": 3, "beauty": 2, "growth": 2, "effectiveness": 3}
    },
    "openlife": {
        "name": "OpenLife",
        "description": "AI-driven life simulation",
        "values": {"honor_life": 3, "truth_seeking": 2, "emergence": 3, "beauty": 2, "growth": 2, "effectiveness": 2}
    },
    "self_improvement": {
        "name": "Self-Improvement",
        "description": "Research and self-improvement",
        "values": {"honor_life": 1, "truth_seeking": 3, "emergence": 2, "beauty": 1, "growth": 3, "effectiveness": 2}
    }
}

# Default allocation based on value alignment
DEFAULT_ALLOCATION = {
    "open-world-selena": 0.35,  # 35% - effectiveness focus
    "selena": 0.25,            # 25% - growth focus
    "openlife": 0.25,           # 25% - emergence focus
    "self_improvement": 0.15    # 15% - truth seeking focus
}


class PriorityEngine:
    def __init__(self):
        self.data = self._load_data()
    
    def _load_data(self) -> dict:
        """Load priority data from file"""
        if os.path.exists(PRIORITY_FILE):
            with open(PRIORITY_FILE, 'r') as f:
                return json.load(f)
        else:
            return self._create_default_data()
    
    def _create_default_data(self) -> dict:
        """Create default priority data"""
        now = datetime.now()
        return {
            "version": "1.0",
            "created": now.isoformat(),
            "last_updated": now.isoformat(),
            "allocation": DEFAULT_ALLOCATION.copy(),
            "value_weights": {k: v["weight"] for k, v in CORE_VALUES.items()},
            "projects": PROJECTS,
            "task_log": [],
            "outcomes": {},
            "learnings": [],
            "total_llm_calls": 0,
            "llm_calls_budget": 4500,
            "target_llm_calls": 4000,
            "buffer_llm_calls": 500,
            "period_hours": 5
        }
    
    def _save_data(self):
        """Save priority data to file"""
        self.data["last_updated"] = datetime.now().isoformat()
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PRIORITY_FILE, 'w') as f:
            json.dump(self.data, f, indent=2)
    
    def get_status(self) -> dict:
        """Get current priority status"""
        allocation = self.data["allocation"]
        
        # Calculate value alignment scores
        project_scores = {}
        for project, alloc in allocation.items():
            if project in self.data["projects"]:
                project_info = self.data["projects"][project]
                value_score = self._calculate_value_alignment(project_info["values"])
                project_scores[project] = {
                    "name": project_info["name"],
                    "allocation": alloc,
                    "allocation_percent": alloc * 100,
                    "value_alignment": value_score,
                    "llm_calls_allocated": int(self.data["target_llm_calls"] * alloc)
                }
        
        # Get recent outcomes
        recent_outcomes = self.data["task_log"][-10:] if self.data["task_log"] else []
        
        return {
            "projects": project_scores,
            "total_allocation": sum(allocation.values()),
            "llm_calls_budget": self.data["llm_calls_budget"],
            "target_llm_calls": self.data["target_llm_calls"],
            "buffer_llm_calls": self.data["buffer_llm_calls"],
            "period_hours": self.data["period_hours"],
            "recent_outcomes": recent_outcomes,
            "learnings_count": len(self.data["learnings"])
        }
    
    def _calculate_value_alignment(self, project_values: dict) -> float:
        """Calculate how well project aligns with core values"""
        total_weighted_score = 0
        total_weight = 0
        
        for value_key, project_score in project_values.items():
            if value_key in self.data["value_weights"]:
                weight = self.data["value_weights"][value_key]
                # Normalize project score (1-3) to (1-10)
                normalized = (project_score / 3.0) * 10
                total_weighted_score += normalized * weight
                total_weight += weight
        
        return round(total_weighted_score / total_weight, 2) if total_weight > 0 else 0
    
    def evaluate_task(self, task_description: str) -> dict:
        """Evaluate a task against values and suggest priority"""
        # For now, use simple heuristics
        # Could be enhanced with LLM analysis
        task_lower = task_description.lower()
        
        # Simple keyword matching
        value_scores = {}
        for value_key, value_info in CORE_VALUES.items():
            score = 5  # default neutral
            if value_key == "honor_life":
                if any(w in task_lower for w in ["life", "health", "living", "survival", "sustainability"]):
                    score = 8
                elif any(w in task_lower for w in ["death", "kill", "destroy", "harm"]):
                    score = 2
            elif value_key == "truth_seeking":
                if any(w in task_lower for w in ["research", "understand", "learn", "analyze", "investigate"]):
                    score = 8
            elif value_key == "emergence":
                if any(w in task_lower for w in ["emergent", "spontaneous", "organic", "evolution", "grow"]):
                    score = 8
            elif value_key == "beauty":
                if any(w in task_lower for w in ["beautiful", "art", "design", "aesthetic", "creative"]):
                    score = 8
            elif value_key == "growth":
                if any(w in task_lower for w in ["improve", "grow", "learn", "develop", "evolve", "enhance"]):
                    score = 8
            elif value_key == "effectiveness":
                if any(w in task_lower for w in ["fix", "bug", "error", "working", "functional", "complete"]):
                    score = 8
            value_scores[value_key] = score
        
        # Calculate overall priority
        total_weight = sum(self.data["value_weights"].values())
        weighted_score = sum(
            score * self.data["value_weights"][key] 
            for key, score in value_scores.items()
        ) / total_weight
        
        return {
            "task": task_description,
            "value_scores": value_scores,
            "priority_score": round(weighted_score, 2),
            "priority_level": "HIGH" if weighted_score >= 7 else "MEDIUM" if weighted_score >= 5 else "LOW",
            "suggested_allocation": self._suggest_project_allocation(value_scores)
        }
    
    def _suggest_project_allocation(self, value_scores: dict) -> dict:
        """Suggest which project this task best supports"""
        project_alignment = {}
        for project_key, project_info in self.data["projects"].items():
            alignment = sum(
                value_scores.get(val, 5) * project_info["values"].get(val, 2)
                for val in value_scores.keys()
            ) / len(value_scores)
            project_alignment[project_key] = round(alignment / 9, 2)  # normalize
        
        # Sort by alignment
        sorted_projects = sorted(
            project_alignment.items(), 
            key=lambda x: x[1], 
            reverse=True
        )
        
        return {
            "primary": sorted_projects[0][0] if sorted_projects else None,
            "secondary": sorted_projects[1][0] if len(sorted_projects) > 1 else None,
            "alignment_scores": project_alignment
        }
    
    def suggest_next_task(self) -> dict:
        """Suggest what to work on next based on allocation and priorities"""
        allocation = self.data["allocation"]
        
        suggestions = []
        for project, alloc in allocation.items():
            if project in self.data["projects"]:
                project_info = self.data["projects"][project]
                llm_calls_allocated = int(self.data["target_llm_calls"] * alloc)
                suggestions.append({
                    "project": project,
                    "name": project_info["name"],
                    "description": project_info["description"],
                    "allocation_percent": round(alloc * 100, 1),
                    "llm_calls_available": llm_calls_allocated,
                    "value_alignment": self._calculate_value_alignment(project_info["values"])
                })
        
        # Sort by allocation (highest first)
        suggestions.sort(key=lambda x: x["allocation_percent"], reverse=True)
        
        return {
            "suggestions": suggestions,
            "current_focus": suggestions[0] if suggestions else None,
            "llm_calls_budget_remaining": self.data["llm_calls_budget"]
        }
    
    def log_task(self, project: str, task: str, outcome: str, llm_calls_used: int = 1):
        """Log a completed task and its outcome"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "project": project,
            "task": task,
            "outcome": outcome,
            "llm_calls_used": llm_calls_used
        }
        
        self.data["task_log"].append(entry)
        
        # Update outcomes
        if project not in self.data["outcomes"]:
            self.data["outcomes"][project] = {"success": 0, "failure": 0, "learning": 0}
        
        if outcome in self.data["outcomes"][project]:
            self.data["outcomes"][project][outcome] += 1
        
        # Update total llm calls
        self.data["total_llm_calls"] += llm_calls_used
        
        # Extract learnings
        if outcome == "learning" or outcome == "failure":
            self.data["learnings"].append({
                "timestamp": datetime.now().isoformat(),
                "project": project,
                "task": task,
                "outcome": outcome
            })
        
        # Keep log manageable
        if len(self.data["task_log"]) > 100:
            self.data["task_log"] = self.data["task_log"][-50:]
        
        if len(self.data["learnings"]) > 50:
            self.data["learnings"] = self.data["learnings"][-25:]
        
        self._save_data()
        
        return {"success": True, "logged": entry}
    
    def evolve_allocation(self) -> dict:
        """Adjust allocation based on outcomes and learnings"""
        outcomes = self.data["outcomes"]
        learnings = self.data["learnings"]
        
        # Calculate success rates
        success_rates = {}
        for project, outcome_counts in outcomes.items():
            total = sum(outcome_counts.values())
            if total > 0:
                success_rates[project] = outcome_counts.get("success", 0) / total
            else:
                success_rates[project] = 0.5  # neutral if no data
        
        # Adjust allocation based on success rate
        adjustment = {}
        for project, rate in success_rates.items():
            if project in self.data["allocation"]:
                # If high success, increase allocation slightly
                # If low success, decrease allocation
                current = self.data["allocation"][project]
                delta = (rate - 0.5) * 0.1  # max 5% adjustment
                adjustment[project] = max(0.05, min(0.5, current + delta))
        
        # Normalize to sum to 1.0
        total = sum(adjustment.values())
        if total > 0:
            for project in adjustment:
                adjustment[project] = adjustment[project] / total
        
        self.data["allocation"] = adjustment
        self._save_data()
        
        return {
            "success": True,
            "success_rates": success_rates,
            "new_allocation": adjustment,
            "message": "Allocation evolved based on outcomes"
        }
    
    def get_learnings(self) -> list:
        """Get recent learnings"""
        return self.data["learnings"][-10:] if self.data["learnings"] else []


def main():
    import sys
    
    engine = PriorityEngine()
    
    if len(sys.argv) < 2:
        print("Usage: python3 priority_engine.py <command>")
        print("Commands:")
        print("  status              - Show current priority status")
        print("  suggest             - Suggest what to work on next")
        print("  evaluate '<task>'   - Evaluate a task against values")
        print("  log --project P --outcome O --task T  - Log a completed task")
        print("  evolve              - Evolve allocation based on outcomes")
        print("  learnings            - Show recent learnings")
        return
    
    cmd = sys.argv[1]
    
    if cmd == "status":
        print(json.dumps(engine.get_status(), indent=2))
    
    elif cmd == "suggest":
        print(json.dumps(engine.suggest_next_task(), indent=2))
    
    elif cmd == "evaluate":
        if len(sys.argv) < 3:
            print("Error: provide task description")
            return
        task = " ".join(sys.argv[2:])
        print(json.dumps(engine.evaluate_task(task), indent=2))
    
    elif cmd == "log":
        project = None
        outcome = "learning"
        task = "general"
        llm_calls = 1
        
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--project" and i + 1 < len(sys.argv):
                project = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--outcome" and i + 1 < len(sys.argv):
                outcome = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--task" and i + 1 < len(sys.argv):
                task = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--llm-calls" and i + 1 < len(sys.argv):
                llm_calls = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        
        if project:
            print(json.dumps(engine.log_task(project, task, outcome, llm_calls), indent=2))
        else:
            print("Error: --project required")
    
    elif cmd == "evolve":
        print(json.dumps(engine.evolve_allocation(), indent=2))
    
    elif cmd == "learnings":
        print(json.dumps(engine.get_learnings(), indent=2))
    
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
