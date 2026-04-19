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
- status (open, in_progress, done, blocked)
- sensitive (boolean - if True, stored in todos.env NOT in git)
- parent_id (optional - for hierarchical todos)
- estimated_llm_calls (optional - estimated LLM calls for this task)
- creator_id (optional - who created this todo)
- conversation_id (optional - which conversation this belongs to)
- agent_id (optional - which agent owns this todo)
- block_reason (optional - reason why this todo is blocked)
- waiting_for (optional - ID of the todo this is waiting for)
- created_at
- updated_at
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional, List

# Configuration
AGENT_ROOT = os.path.expanduser("~/openclaw/workspace/selena-project")
DATA_DIR = os.path.join(AGENT_ROOT, "data")
TODO_FILE = os.path.join(DATA_DIR, "todos.json")
SENSITIVE_TODO_FILE = os.path.join(DATA_DIR, "todos.env")  # NOT in git - sensitive todos
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
MAX_BACKUPS = 5  # Keep last N backups


class TodoManager:
    """
    Manages todos/loose ends for Selena v2.
    Supports:
    - Regular todos (stored in todos.json - git-friendly)
    - Sensitive todos (stored in todos.env - NOT in git)
    - Hierarchical todos (parent-child relationships via parent_id)
    - Estimated LLM calls tracking
    """
    
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.todos = self._load_todos()
        self.sensitive_todos = self._load_sensitive_todos()
    
    def _load_todos(self) -> list:
        """Load non-sensitive todos from file."""
        if os.path.exists(TODO_FILE):
            try:
                with open(TODO_FILE, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _load_sensitive_todos(self) -> list:
        """Load sensitive todos from file (NOT in git)."""
        if os.path.exists(SENSITIVE_TODO_FILE):
            try:
                with open(SENSITIVE_TODO_FILE, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _backup_todos(self, todos: list, filename: str):
        """Create a timestamped backup before saving."""
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"todos_backup_{timestamp}_{filename}"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        try:
            with open(filepath, 'r') as src:
                with open(backup_path, 'w') as dst:
                    dst.write(src.read())
            self._cleanup_old_backups()
        except Exception as e:
            print(f"Backup failed: {e}")

    def _cleanup_old_backups(self):
        """Remove old backups, keeping only the most recent MAX_BACKUPS."""
        if not os.path.exists(BACKUP_DIR):
            return
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("todos_backup_")])
        while len(backups) > MAX_BACKUPS:
            old_backup = backups.pop(0)
            try:
                os.remove(os.path.join(BACKUP_DIR, old_backup))
            except:
                pass

    def list_backups(self) -> List[str]:
        """List available backups."""
        if not os.path.exists(BACKUP_DIR):
            return []
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("todos_backup_")], reverse=True)
        return backups

    def restore_latest(self) -> bool:
        """Restore from the most recent backup. Returns True if successful."""
        backups = self.list_backups()
        if not backups:
            return False
        latest = os.path.join(BACKUP_DIR, backups[0])
        try:
            with open(latest, 'r') as f:
                data = json.load(f)
                # Determine which type of backup this is
                if "_todos.env" in backups[0]:
                    self.sensitive_todos = data
                    self._save_sensitive_todos()
                else:
                    self.todos = data
                    self._save_todos()
            return True
        except Exception as e:
            print(f"Restore failed: {e}")
            return False

    def _save_todos(self):
        """Save non-sensitive todos to file (creates backup first)."""
        self._backup_todos(self.todos, "todos.json")
        with open(TODO_FILE, 'w') as f:
            json.dump(self.todos, f, indent=2)
    
    def _save_sensitive_todos(self):
        """Save sensitive todos to file (creates backup first)."""
        self._backup_todos(self.sensitive_todos, "todos.env")
        with open(SENSITIVE_TODO_FILE, 'w') as f:
            json.dump(self.sensitive_todos, f, indent=2)
    
    def _now(self) -> str:
        """Get current ISO timestamp."""
        return datetime.now().isoformat()
    
    def _get_all_todos(self) -> list:
        """Get all todos (both regular and sensitive)."""
        return self.todos + self.sensitive_todos
    
    def _get_todo_list(self, sensitive: bool) -> list:
        """Get the appropriate todo list based on sensitive flag."""
        return self.sensitive_todos if sensitive else self.todos
    
    def _save_todo_list(self, sensitive: bool):
        """Save the appropriate todo list based on sensitive flag."""
        if sensitive:
            self._save_sensitive_todos()
        else:
            self._save_todos()
    
    def add_todo(self, short_desc: str, long_desc: str = "", priority: int = 5, 
                 sensitive: bool = False, parent_id: Optional[str] = None,
                 estimated_llm_calls: Optional[int] = None,
                 creator_id: Optional[str] = None,
                 conversation_id: Optional[str] = None,
                 agent_id: Optional[str] = None) -> dict:
        """
        Add a new todo.
        
        Args:
            short_desc: Brief title (required)
            long_desc: Detailed description (optional)
            priority: 1-10, 10 = highest (default 5)
            sensitive: If True, stored in todos.env NOT in git (default False)
            parent_id: Optional parent todo ID for hierarchical todos
            estimated_llm_calls: Optional estimated LLM calls for this task
            creator_id: Optional ID of who created this todo
            conversation_id: Optional ID of the conversation this belongs to
            agent_id: Optional ID of the agent that owns this todo
        
        Returns:
            The created todo dict
        """
        todo = {
            "id": str(uuid.uuid4())[:8],
            "short_desc": short_desc,
            "long_desc": long_desc,
            "priority": max(1, min(10, priority)),  # Clamp to 1-10
            "status": "open",
            "sensitive": sensitive,
            "parent_id": parent_id,  # None means top-level todo
            "estimated_llm_calls": estimated_llm_calls,
            "creator_id": creator_id,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "block_reason": None,  # Reason why blocked (if status is blocked)
            "waiting_for": None,   # ID of todo this is waiting for
            "deleted_at": None,    # Soft delete timestamp (None = not deleted)
            "created_at": self._now(),
            "updated_at": self._now()
        }
        
        todo_list = self._get_todo_list(sensitive)
        todo_list.append(todo)
        self._save_todo_list(sensitive)
        
        # Also add to in-memory list
        if sensitive:
            self.sensitive_todos.append(todo)
        else:
            self.todos.append(todo)
        
        return todo
    
    def get_todo(self, todo_id: str) -> Optional[dict]:
        """Get a specific todo by ID (searches both regular and sensitive)."""
        for todo in self.todos:
            if todo["id"] == todo_id:
                return todo
        for todo in self.sensitive_todos:
            if todo["id"] == todo_id:
                return todo
        return None
    
    def get_todo_list(self, sensitive: bool) -> list:
        """Get todos from a specific list (regular or sensitive)."""
        return self._get_todo_list(sensitive)
    
    def get_all_todos(self, status: Optional[str] = None, sort_by: str = "priority",
                      include_children: bool = True, sensitive: Optional[bool] = None,
                      include_deleted: bool = False, search: Optional[str] = None) -> list:
        """
        Get all todos, optionally filtered by status.
        
        Args:
            status: Filter by status (open, in_progress, done). None = all.
            sort_by: Sort by "priority", "created", or "updated" (default: priority)
            include_children: If True, include child todos under their parents
            sensitive: If None, include all. If True, only sensitive. If False, only non-sensitive.
            include_deleted: If True, include soft-deleted todos (deleted_at is not null)
            search: Filter by short_desc (case-insensitive partial match)
        
        Returns:
            List of todo dicts
        """
        # Select which lists to use
        if sensitive is None:
            todos = self._get_all_todos()
        elif sensitive:
            todos = self._get_todo_list(True)
        else:
            todos = self._get_todo_list(False)
        
        # Filter by deleted status
        if not include_deleted:
            todos = [t for t in todos if not t.get("deleted_at")]
        
        # Filter by status
        if status:
            todos = [t for t in todos if t["status"] == status]
        
        # Filter by search query (search in short_desc)
        if search:
            search_lower = search.lower()
            todos = [t for t in todos if search_lower in t.get("short_desc", "").lower()]
        
        # Sort
        if sort_by == "priority":
            todos = sorted(todos, key=lambda t: t["priority"], reverse=True)
        elif sort_by == "created":
            todos = sorted(todos, key=lambda t: t["created_at"], reverse=True)
        elif sort_by == "updated":
            todos = sorted(todos, key=lambda t: t["updated_at"], reverse=True)
        
        # If include_children, restructure to show hierarchy
        if include_children:
            # Separate parent todos from child todos
            parents = [t for t in todos if t.get("parent_id") is None]
            children_map = {}
            for t in todos:
                if t.get("parent_id"):
                    if t["parent_id"] not in children_map:
                        children_map[t["parent_id"]] = []
                    children_map[t["parent_id"]].append(t)
            
            # Add children to parents
            result = []
            for parent in parents:
                result.append(parent)
                if parent["id"] in children_map:
                    result.extend(children_map[parent["id"]])
            return result
        
        return todos
    
    def get_children(self, parent_id: str) -> list:
        """Get all child todos of a parent todo."""
        all_todos = self._get_all_todos()
        children = [t for t in all_todos if t.get("parent_id") == parent_id]
        return sorted(children, key=lambda t: t["priority"], reverse=True)
    
    def split_todo(self, todo_id: str, subtasks: List[dict]) -> Optional[list]:
        """
        Split a big todo into smaller subtasks.
        
        Args:
            todo_id: ID of the parent todo to split
            subtasks: List of dicts with 'short_desc', 'long_desc', 'priority', 'estimated_llm_calls'
        
        Returns:
            List of created subtask dicts, or None if parent not found
        """
        parent = self.get_todo(todo_id)
        if not parent:
            return None
        
        created = []
        for task in subtasks:
            todo = self.add_todo(
                short_desc=task.get("short_desc", "Subtask"),
                long_desc=task.get("long_desc", ""),
                priority=task.get("priority", parent.get("priority", 5)),
                sensitive=parent.get("sensitive", False),
                parent_id=todo_id,
                estimated_llm_calls=task.get("estimated_llm_calls")
            )
            created.append(todo)
        
        return created
    
    def update_todo(self, todo_id: str, **kwargs) -> Optional[dict]:
        """
        Update a todo.
        
        Args:
            todo_id: ID of todo to update
            **kwargs: Fields to update (short_desc, long_desc, priority, status, sensitive, parent_id, estimated_llm_calls, creator_id, conversation_id, agent_id, block_reason, waiting_for)
        
        Returns:
            Updated todo dict or None if not found
        """
        # Find in regular todos
        for todo in self.todos:
            if todo["id"] == todo_id:
                return self._update_todo_in_list(todo, self.todos, **kwargs)
        
        # Find in sensitive todos
        for todo in self.sensitive_todos:
            if todo["id"] == todo_id:
                return self._update_todo_in_list(todo, self.sensitive_todos, **kwargs)
        
        return None
    
    def _update_todo_in_list(self, todo: dict, todo_list: list, **kwargs) -> dict:
        """Update a todo in a specific list."""
        # Handle restore parameter (sets deleted_at to None)
        if kwargs.get("restore"):
            todo["deleted_at"] = None
            todo["updated_at"] = self._now()
            if todo in self.todos:
                self._save_todos()
            else:
                self._save_sensitive_todos()
            return todo
        
        # Update allowed fields
        allowed = ["short_desc", "long_desc", "priority", "status", "sensitive", "parent_id", "estimated_llm_calls", "creator_id", "conversation_id", "agent_id", "block_reason", "waiting_for", "deleted_at"]
        for key in allowed:
            if key in kwargs:
                if key == "priority":
                    todo[key] = max(1, min(10, kwargs[key]))
                elif key == "sensitive":
                    # Handle moving between lists
                    new_sensitive = kwargs[key]
                    if todo.get("sensitive") != new_sensitive:
                        # Move between lists
                        todo_list.remove(todo)
                        todo[key] = new_sensitive
                        target_list = self.sensitive_todos if new_sensitive else self.todos
                        target_list.append(todo)
                        self._save_todos()
                        self._save_sensitive_todos()
                        todo["updated_at"] = self._now()
                        return todo
                else:
                    todo[key] = kwargs[key]
        
        todo["updated_at"] = self._now()
        
        # Save appropriate list
        if todo in self.todos:
            self._save_todos()
        else:
            self._save_sensitive_todos()
        
        return todo
    
    def delete_todo(self, todo_id: str, delete_children: bool = True) -> bool:
        """
        Delete a todo by ID. Returns True if deleted, False if not found.
        
        Args:
            todo_id: ID of todo to delete
            delete_children: If True, also delete all child todos
        """
        # Try regular todos first
        for i, todo in enumerate(self.todos):
            if todo["id"] == todo_id:
                if delete_children:
                    self._delete_children(todo_id, self.todos)
                self.todos.pop(i)
                self._save_todos()
                return True
        
        # Try sensitive todos
        for i, todo in enumerate(self.sensitive_todos):
            if todo["id"] == todo_id:
                if delete_children:
                    self._delete_children(todo_id, self.sensitive_todos)
                self.sensitive_todos.pop(i)
                self._save_sensitive_todos()
                return True
        
        return False
    
    def _delete_children(self, parent_id: str, todo_list: list):
        """Delete all children of a parent todo from a specific list."""
        children = [t for t in todo_list if t.get("parent_id") == parent_id]
        for child in children:
            # Recursively delete grandchildren
            self._delete_children(child["id"], todo_list)
            todo_list.remove(child)
    
    def mark_done(self, todo_id: str) -> Optional[dict]:
        """Mark a todo as done."""
        return self.update_todo(todo_id, status="done")
    
    def mark_in_progress(self, todo_id: str) -> Optional[dict]:
        """Mark a todo as in progress."""
        return self.update_todo(todo_id, status="in_progress")
    
    def mark_blocked(self, todo_id: str, block_reason: str = "", waiting_for: Optional[str] = None) -> Optional[dict]:
        """
        Mark a todo as blocked.
        
        Args:
            todo_id: ID of todo to block
            block_reason: Reason why it's blocked
            waiting_for: ID of the todo this is waiting for (optional)
        
        Returns:
            Updated todo dict or None if not found
        """
        return self.update_todo(todo_id, status="blocked", block_reason=block_reason, waiting_for=waiting_for)
    
    def unblock(self, todo_id: str) -> Optional[dict]:
        """
        Unblock a todo (set status back to open and clear block_reason/waiting_for).
        
        Args:
            todo_id: ID of todo to unblock
        
        Returns:
            Updated todo dict or None if not found
        """
        return self.update_todo(todo_id, status="open", block_reason=None, waiting_for=None)
    
    def get_summary(self, sensitive: Optional[bool] = None) -> dict:
        """
        Get a summary of all todos.
        
        Args:
            sensitive: If None, all. If True, only sensitive. If False, only non-sensitive.
        """
        if sensitive is None:
            all_todos = self._get_all_todos()
        elif sensitive:
            all_todos = self._get_todo_list(True)
        else:
            all_todos = self._get_todo_list(False)
        
        open_todos = [t for t in all_todos if t["status"] == "open" and t.get("parent_id") is None]
        in_progress = [t for t in all_todos if t["status"] == "in_progress" and t.get("parent_id") is None]
        completed = [t for t in all_todos if t["status"] == "completed" and t.get("parent_id") is None]
        blocked = [t for t in all_todos if t["status"] == "blocked" and t.get("parent_id") is None]
        done = [t for t in all_todos if t["status"] == "done"]
        
        # Calculate total estimated LLM calls
        total_llm_calls = sum(t.get("estimated_llm_calls", 0) or 0 for t in all_todos)
        open_llm_calls = sum(t.get("estimated_llm_calls", 0) or 0 for t in open_todos)
        
        # Get top 3 by priority
        top_priority = sorted(open_todos, key=lambda t: t["priority"], reverse=True)[:3]
        
        return {
            "total": len([t for t in all_todos if t.get("parent_id") is None]),
            "open": len(open_todos),
            "in_progress": len(in_progress),
            "completed": len(completed),
            "blocked": len(blocked),
            "done": len(done),
            "total_llm_calls": total_llm_calls,
            "open_llm_calls": open_llm_calls,
            "top_priority": top_priority
        }


# Global instance
todo_manager = TodoManager()
