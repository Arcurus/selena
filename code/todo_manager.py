#!/usr/bin/env python3
"""
Todo Manager for Selena v2
==========================
Manages loose ends / todos with priority, short description, and long description.

Each todo has:
- id (auto-generated UUID)
- priority (1-10, 10 = highest)
- short_desc (brief title)
- long_desc (detailed description)
- status (open, in_progress, done)
- created_at
- updated_at
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional

# Configuration
AGENT_ROOT = os.path.expanduser("~/openclaw/workspace/selena")
DATA_DIR = os.path.join(AGENT_ROOT, "data")
TODO_FILE = os.path.join(DATA_DIR, "todos.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
MAX_BACKUPS = 5  # Keep last N backups


class TodoManager:
    """
    Manages todos/loose ends for Selena v2.
    """
    
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.todos = self._load_todos()
    
    def _load_todos(self) -> list:
        """Load todos from file."""
        if os.path.exists(TODO_FILE):
            try:
                with open(TODO_FILE, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _backup_todos(self):
        """Create a timestamped backup before saving."""
        if not os.path.exists(TODO_FILE):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"todos_backup_{timestamp}.json"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        try:
            with open(TODO_FILE, 'r') as src:
                with open(backup_path, 'w') as dst:
                    dst.write(src.read())
            # Clean up old backups, keeping only MAX_BACKUPS
            self._cleanup_old_backups()
        except Exception as e:
            print(f"Backup failed: {e}")

    def _cleanup_old_backups(self):
        """Remove old backups, keeping only the most recent MAX_BACKUPS."""
        if not os.path.exists(BACKUP_DIR):
            return
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("todos_backup_") and f.endswith(".json")])
        while len(backups) > MAX_BACKUPS:
            old_backup = backups.pop(0)
            try:
                os.remove(os.path.join(BACKUP_DIR, old_backup))
            except:
                pass

    def list_backups(self):
        """List available backups."""
        if not os.path.exists(BACKUP_DIR):
            return []
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("todos_backup_") and f.endswith(".json")], reverse=True)
        return backups

    def restore_latest(self) -> bool:
        """Restore from the most recent backup. Returns True if successful."""
        backups = self.list_backups()
        if not backups:
            return False
        latest = os.path.join(BACKUP_DIR, backups[0])
        try:
            with open(latest, 'r') as f:
                self.todos = json.load(f)
            self._save_todos()
            return True
        except Exception as e:
            print(f"Restore failed: {e}")
            return False

    def _save_todos(self):
        """Save todos to file (creates backup first)."""
        self._backup_todos()  # Create backup before saving
        with open(TODO_FILE, 'w') as f:
            json.dump(self.todos, f, indent=2)
    
    def _now(self) -> str:
        """Get current ISO timestamp."""
        return datetime.now().isoformat()
    
    def add_todo(self, short_desc: str, long_desc: str = "", priority: int = 5) -> dict:
        """
        Add a new todo.
        
        Args:
            short_desc: Brief title (required)
            long_desc: Detailed description (optional)
            priority: 1-10, 10 = highest (default 5)
        
        Returns:
            The created todo dict
        """
        todo = {
            "id": str(uuid.uuid4())[:8],
            "short_desc": short_desc,
            "long_desc": long_desc,
            "priority": max(1, min(10, priority)),  # Clamp to 1-10
            "status": "open",
            "created_at": self._now(),
            "updated_at": self._now()
        }
        self.todos.append(todo)
        self._save_todos()
        return todo
    
    def get_todo(self, todo_id: str) -> Optional[dict]:
        """Get a specific todo by ID."""
        for todo in self.todos:
            if todo["id"] == todo_id:
                return todo
        return None
    
    def get_all_todos(self, status: Optional[str] = None, sort_by: str = "priority") -> list:
        """
        Get all todos, optionally filtered by status.
        
        Args:
            status: Filter by status (open, in_progress, done). None = all.
            sort_by: Sort by "priority", "created", or "updated" (default: priority)
        
        Returns:
            List of todo dicts
        """
        todos = self.todos
        
        # Filter by status
        if status:
            todos = [t for t in todos if t["status"] == status]
        
        # Sort
        if sort_by == "priority":
            todos = sorted(todos, key=lambda t: t["priority"], reverse=True)
        elif sort_by == "created":
            todos = sorted(todos, key=lambda t: t["created_at"], reverse=True)
        elif sort_by == "updated":
            todos = sorted(todos, key=lambda t: t["updated_at"], reverse=True)
        
        return todos
    
    def update_todo(self, todo_id: str, **kwargs) -> Optional[dict]:
        """
        Update a todo.
        
        Args:
            todo_id: ID of todo to update
            **kwargs: Fields to update (short_desc, long_desc, priority, status)
        
        Returns:
            Updated todo dict or None if not found
        """
        todo = self.get_todo(todo_id)
        if not todo:
            return None
        
        # Update allowed fields
        allowed = ["short_desc", "long_desc", "priority", "status"]
        for key in allowed:
            if key in kwargs:
                if key == "priority":
                    todo[key] = max(1, min(10, kwargs[key]))
                else:
                    todo[key] = kwargs[key]
        
        todo["updated_at"] = self._now()
        self._save_todos()
        return todo
    
    def delete_todo(self, todo_id: str) -> bool:
        """Delete a todo by ID. Returns True if deleted, False if not found."""
        for i, todo in enumerate(self.todos):
            if todo["id"] == todo_id:
                self.todos.pop(i)
                self._save_todos()
                return True
        return False
    
    def mark_done(self, todo_id: str) -> Optional[dict]:
        """Mark a todo as done."""
        return self.update_todo(todo_id, status="done")
    
    def mark_in_progress(self, todo_id: str) -> Optional[dict]:
        """Mark a todo as in progress."""
        return self.update_todo(todo_id, status="in_progress")
    
    def get_summary(self) -> dict:
        """Get a summary of all todos."""
        open_todos = [t for t in self.todos if t["status"] == "open"]
        in_progress = [t for t in self.todos if t["status"] == "in_progress"]
        done = [t for t in self.todos if t["status"] == "done"]
        
        # Get top 3 by priority
        top_priority = sorted(open_todos, key=lambda t: t["priority"], reverse=True)[:3]
        
        return {
            "total": len(self.todos),
            "open": len(open_todos),
            "in_progress": len(in_progress),
            "done": len(done),
            "top_priority": top_priority
        }


# Global instance
todo_manager = TodoManager()
