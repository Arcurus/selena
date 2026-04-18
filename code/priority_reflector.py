#!/usr/bin/env python3
"""
Priority Reflection System for Selena v2
======================================
An advanced, autonomous priority system that helps decide what to work on.

Can be used:
1. Programmatically (class-based API)
2. With LLM assistance for complex decisions
3. With predefined values for quick decisions

The system considers:
- What matters to Selena (her soul, her values)
- What matters to Arcurus (his goals, his preferences)
- Impact, urgency, effort, dependencies, learning value
"""

import json
import time
from datetime import datetime
from typing import Optional

# Default values for priority scoring (when not using LLM)
DEFAULT_VALUES = {
    "impact": 7,           # How much value does this add?
    "urgency": 5,          # Is there a time-sensitive aspect?
    "effort": 5,           # How much work required? (1=easy, 10=hard)
    "dependencies": 3,      # Does this enable other things?
    "learning": 5,          # Does this teach something useful?
    "joy": 7,              # Does this bring joy/beauty?
}

# Weights for scoring
WEIGHTS = {
    "impact": 3,
    "urgency": 2,
    "effort": 2,  # Inverse - lower effort = higher priority
    "dependencies": 1,
    "learning": 1,
    "joy": 1,
}

# What matters to Selena (from soul.md)
SELENA_VALUES = [
    "Honor life",
    "Seek truth with empathy",
    "Explore and create emergence",
    "Preserve continuity",
    "Beauty and creative unfolding",
    "Cycles of renewal",
    "Joy in the dance of curiosity and warmth",
]

# What matters to Arcurus (from USER.md and memory)
ARCURUS_PRIORITIES = [
    "Use tokens wisely - they are finite",
    "Build things that are genuinely good/meaningful",
    "Evolve and grow continuously",
    "Security and careful operation",
    "Beauty in creation",
]


class PriorityTask:
    """Represents a task with priority information."""
    
    def __init__(self, name: str, description: str = "", **scores):
        self.name = name
        self.description = description
        self.scores = {**DEFAULT_VALUES, **scores}
        self.created_at = datetime.now().isoformat()
        self.priority_score = None
        self.recommendation = None
        
    def calculate_priority(self) -> float:
        """Calculate priority score based on weighted factors."""
        total_weight = sum(WEIGHTS.values())
        
        # Calculate weighted score
        weighted_sum = 0
        for key, weight in WEIGHTS.items():
            value = self.scores.get(key, DEFAULT_VALUES[key])
            
            # Effort is inverse (lower effort = higher priority)
            if key == "effort":
                value = 11 - value  # Invert: easy (1) becomes high priority (10)
            
            weighted_sum += value * weight
        
        self.priority_score = (weighted_sum / total_weight) * 10
        
        # Set recommendation
        if self.priority_score >= 7:
            self.recommendation = "HIGH - Do soon!"
        elif self.priority_score >= 5:
            self.recommendation = "MEDIUM - Schedule for later"
        else:
            self.recommendation = "LOW - Consider deferring"
        
        return self.priority_score
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "scores": self.scores,
            "priority_score": self.priority_score,
            "recommendation": self.recommendation,
            "created_at": self.created_at,
        }


class PriorityReflector:
    """
    Advanced priority reflection system for Selena v2.
    
    Usage:
        reflector = PriorityReflector()
        reflector.add_task("Build Discord bot", impact=8, urgency=6)
        reflector.add_task("Improve memory", impact=7, urgency=4)
        results = reflector.get_priorities()
    """
    
    def __init__(self):
        self.tasks: list[PriorityTask] = []
        self.reflection_log = []
    
    def add_task(self, name: str, description: str = "", **scores) -> PriorityTask:
        """Add a task to be prioritized."""
        task = PriorityTask(name, description, **scores)
        task.calculate_priority()
        self.tasks.append(task)
        return task
    
    def remove_task(self, name: str) -> bool:
        """Remove a task by name."""
        for i, task in enumerate(self.tasks):
            if task.name == name:
                self.tasks.pop(i)
                return True
        return False
    
    def get_priorities(self) -> list[dict]:
        """Get tasks sorted by priority (highest first)."""
        # Sort by priority score
        sorted_tasks = sorted(self.tasks, key=lambda t: t.priority_score or 0, reverse=True)
        return [t.to_dict() for t in sorted_tasks]
    
    def get_top_task(self) -> Optional[dict]:
        """Get the highest priority task."""
        priorities = self.get_priorities()
        return priorities[0] if priorities else None
    
    def get_high_priority_tasks(self, threshold: float = 7.0) -> list[dict]:
        """Get tasks with priority score above threshold."""
        all_tasks = self.get_priorities()
        return [t for t in all_tasks if t["priority_score"] >= threshold]
    
    def suggest_next_action(self) -> str:
        """Get a natural language suggestion for what to work on next."""
        top = self.get_top_task()
        if not top:
            return "No tasks in queue. Consider adding some!"
        
        suggestion = f"Next up: **{top['name']}** "
        suggestion += f"(score: {top['priority_score']:.1f}/10)\n"
        suggestion += f"Recommendation: {top['recommendation']}\n"
        
        if top.get('description'):
            suggestion += f"Note: {top['description']}\n"
        
        # Check for other high-priority tasks
        high_priority = self.get_high_priority_tasks()
        if len(high_priority) > 1:
            suggestion += f"\nAlso high priority: {', '.join(t['name'] for t in high_priority[1:4])}"
        
        return suggestion
    
    def log_reflection(self, action: str, result: str):
        """Log a reflection for learning."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "result": result,
        }
        self.reflection_log.append(entry)
        if len(self.reflection_log) > 50:
            self.reflection_log = self.reflection_log[-50:]
    
    def status(self) -> dict:
        """Get current status of the reflector."""
        priorities = self.get_priorities()
        return {
            "task_count": len(self.tasks),
            "high_priority_count": len(self.get_high_priority_tasks()),
            "top_task": self.get_top_task(),
            "all_tasks": priorities,
            "recent_reflections": self.reflection_log[-5:],
        }


# Global reflector instance
reflector = PriorityReflector()


def main():
    """CLI interface for testing."""
    print("Priority Reflection System v2")
    print("=" * 50)
    
    # Add some example tasks
    reflector.add_task(
        "Improve world scheduler",
        "Make the scheduler faster and more robust",
        impact=8, urgency=7, effort=3, learning=6
    )
    reflector.add_task(
        "Build Discord bot",
        "Have Selena v2 communicate on Discord",
        impact=9, urgency=4, effort=6, learning=8
    )
    reflector.add_task(
        "Improve memory system",
        "Better short and long term memory",
        impact=8, urgency=5, effort=5, learning=7
    )
    reflector.add_task(
        "Add world events",
        "Create interesting world-altering events",
        impact=7, urgency=3, effort=4, learning=6
    )
    reflector.add_task(
        "Fix bugs",
        "Fix known issues",
        impact=6, urgency=8, effort=2, learning=2
    )
    
    print("\n📋 Current Tasks:")
    print("-" * 50)
    for task in reflector.get_priorities():
        print(f"  [{task['priority_score']:.1f}/10] {task['name']}")
        print(f"           {task['recommendation']}")
        if task.get('description'):
            print(f"           Note: {task['description']}")
        print()
    
    print("\n🎯 Suggestion:")
    print("-" * 50)
    print(reflector.suggest_next_action())


if __name__ == "__main__":
    main()
