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
- status (open, in_progress, completed, blocked, done)
- sensitive (boolean - if True, stored in todos.env NOT in git)
- parent_id (optional - for hierarchical todos)
- estimated_llm_calls (optional - estimated LLM calls for this task)
- creator_id (optional - who created this todo)
- conversation_id (optional - which conversation this belongs to)
- agent_id (optional - which agent owns this todo)
- block_reason (optional - reason why this todo is blocked)
- waiting_for (optional - ID of the todo this is waiting for)
- completed_at (ISO timestamp set automatically by status transitions; see _apply_completed_at_rule)
- created_at
- updated_at
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional, List

# Sentinel for distinguishing "field not passed" from "field passed as None".
# Used by _update_todo_in_list so an explicit completed_at=None from the
# caller still wins over the auto-rule.
_SENTINEL_UNSET = object()

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
        # Track file mtime + size so callers can detect external edits without
        # reading the file. Updated on every load and save. (added 2026-06-05
        # per selena-project-worker to address loose-end todo 31e876a4.)
        self._todos_signature = self._file_signature(TODO_FILE)
        self._sensitive_signature = self._file_signature(SENSITIVE_TODO_FILE)

    @staticmethod
    def _file_signature(path: str) -> tuple:
        """Return (mtime_ns, size) for `path`, or (0, 0) if missing/unreadable.
        Cheap stat() call — used to detect external file edits.
        """
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, 0)

    def is_stale(self) -> bool:
        """True if the on-disk file has changed since the last load/save.
        Cheap check (two stat() calls) — callers can use this to decide
        whether to call reload() before a read-heavy operation.
        """
        return (self._file_signature(TODO_FILE) != self._todos_signature
                or self._file_signature(SENSITIVE_TODO_FILE) != self._sensitive_signature)

    def reload(self) -> dict:
        """Re-read todos from disk into memory. Returns a small summary
        dict so callers (and tests) can confirm what changed. Safe to
        call any time — does not touch disk writes, only reads.

        Use this after manually editing data/todos.json (or after a
        backup restore, or any time an external process mutated the
        file). The next save() will pick up the freshly-loaded state.

        Returns: {"regular": int, "sensitive": int, "stale": bool}
        """
        was_stale = self.is_stale()
        self.todos = self._load_todos()
        self.sensitive_todos = self._load_sensitive_todos()
        self._todos_signature = self._file_signature(TODO_FILE)
        self._sensitive_signature = self._file_signature(SENSITIVE_TODO_FILE)
        return {
            "regular": len(self.todos),
            "sensitive": len(self.sensitive_todos),
            "stale": was_stale,
        }

    def _load_todos(self) -> list:
        """Load non-sensitive todos from file."""
        if os.path.exists(TODO_FILE):
            try:
                with open(TODO_FILE, 'r') as f:
                    data = json.load(f)
                # Accept both list (canonical) and dict {"todos": [...]} (some
                # earlier writers saved the API response shape). Normalize to list.
                if isinstance(data, dict) and 'todos' in data:
                    return data['todos']
                if isinstance(data, list):
                    return data
                return []
            except:
                return []
        return []

    def _load_sensitive_todos(self) -> list:
        """Load sensitive todos from file (NOT in git)."""
        if os.path.exists(SENSITIVE_TODO_FILE):
            try:
                with open(SENSITIVE_TODO_FILE, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict) and 'todos' in data:
                    return data['todos']
                if isinstance(data, list):
                    return data
                return []
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
        # Refresh signature so is_stale() stays accurate after our own writes
        # (added 2026-06-05 per selena-project-worker — see reload()).
        self._todos_signature = self._file_signature(TODO_FILE)

    def _save_sensitive_todos(self):
        """Save sensitive todos to file (creates backup first)."""
        self._backup_todos(self.sensitive_todos, "todos.env")
        with open(SENSITIVE_TODO_FILE, 'w') as f:
            json.dump(self.sensitive_todos, f, indent=2)
        self._sensitive_signature = self._file_signature(SENSITIVE_TODO_FILE)

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

    def find_open_by_signature(self, short_desc: str, creator_id: str) -> Optional[dict]:
        """Return an existing OPEN todo with matching (short_desc, creator_id),
        or None.  Added 2026-06-03 as a defensive dedup for the API: if the
        same caller asks to add the same short_desc twice in a row, return
        the existing record instead of creating a second one.  Guards against
        client-side double-submits AND a future regression of the
        double-append bug fixed in commit afddbfa.

        Match rule: exact short_desc + exact creator_id + status in
        ('open', 'in_progress') + not soft-deleted.
        """
        target = (short_desc or "").strip()
        cid = (creator_id or "").strip()
        if not target or not cid:
            return None
        for todo in self._get_all_todos():
            if todo.get("deleted_at"):
                continue
            if todo.get("status") not in ("open", "in_progress"):
                continue
            if (todo.get("short_desc") or "").strip() == target \
                    and (todo.get("creator_id") or "").strip() == cid:
                return todo
        return None

    def add_todo(self, short_desc: str, long_desc: str = "", priority: int = 5,
                 sensitive: bool = False, parent_id: Optional[str] = None,
                 estimated_llm_calls: Optional[int] = None,
                 creator_id: Optional[str] = None,
                 conversation_id: Optional[str] = None,
                 agent_id: Optional[str] = None,
                 project: Optional[str] = None,
                 agent_owner: Optional[str] = None,
                 what_happened: Optional[str] = None,
                 dedup: bool = False) -> dict:
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
            project: Optional project tag (e.g. "selena-project", "selena-project-lunar",
                "open-world-selena", "openlife", "unassigned"). Used by project-worker
                crons to filter their work. (2026-06-03 per Arcurus.)
            agent_owner: Optional name of the agent/worker currently working on or
                finishing this todo. Same convention as the worker cron names
                (e.g. "selena-project-worker", "selena-project-lunar-worker",
                "selena-slow-heartbeat", "arcurus" for human signoff).
            what_happened: Optional free-text summary of what was done. **Required**
                when an agent/worker marks the todo as `completed` or `done` —
                captures the actual outcome so reviewers don't have to read the
                full session transcript. (2026-06-03 per Arcurus.)
            dedup: When True (default False for backward compat), first run
                find_open_by_signature(short_desc, creator_id) and return the
                existing open/in_progress todo if one matches instead of
                creating a new record. Saves a save_todo_list() call and an
                API round-trip when the caller would have caught the duplicate
                anyway. Recommended for crons and any bulk-add path. Added
                2026-06-05 per selena-project-worker (implements
                heartbeat.md §3c recommendation).

        Returns:
            The created todo dict. If dedup=True and a matching open todo
            already exists, returns that existing todo (a fresh copy with
            no new id, no new created_at).
        """
        # Defensive dedup (added 2026-06-05): if dedup=True and a matching
        # open todo exists, return it instead of creating a duplicate.
        # API handlers (do_GET /api/todos/mark-done and do_POST /api/todos/add)
        # already do this at the API layer; this moves the check into the
        # manager so non-API callers (crons, scripts) get the same protection.
        if dedup and creator_id and short_desc:
            existing = self.find_open_by_signature(short_desc, creator_id)
            if existing:
                return existing
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
            "project": project,            # NEW (2026-06-03)
            "agent_owner": agent_owner,    # NEW (2026-06-03)
            "what_happened": what_happened,  # NEW (2026-06-03) — see add/update_todo docstring
            "block_reason": None,  # Reason why blocked (if status is blocked)
            "waiting_for": None,   # ID of todo this is waiting for
            "completed_at": None,  # Auto-set on completed/done transitions (see _apply_completed_at_rule)
            "deleted_at": None,    # Soft delete timestamp (None = not deleted)
            "created_at": self._now(),
            "updated_at": self._now()
        }

        todo_list = self._get_todo_list(sensitive)
        # todo_list IS self.todos (or self.sensitive_todos) — same list reference.
        # Appending to it already mutates the in-memory list, so we don't need a
        # second append after the save. (2026-06-03: previous double-append
        # caused every add_todo to insert 2 records in memory, which then got
        # flushed to disk on the next save — e.g. on mark-done — producing
        # duplicate IDs in data/todos.json.)
        todo_list.append(todo)
        self._save_todo_list(sensitive)

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
                      include_deleted: bool = False, search: Optional[str] = None,
                      agent_owner: Optional[str] = None,
                      project: Optional[str] = None) -> list:
        """
        Get all todos, optionally filtered by status.

        Args:
            status: Filter by status (open, in_progress, done). None = all.
            sort_by: Sort by "priority", "created", or "updated" (default: priority)
            include_children: If True, include child todos under their parents
            sensitive: If None, include all. If True, only sensitive. If False, only non-sensitive.
            include_deleted: If True, include soft-deleted todos (deleted_at is not null)
            search: Filter by short_desc (case-insensitive partial match)
            agent_owner: Filter by assigned agent (agent_owner field). None = all.
            project: Filter by project tag. None = all.

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

        # Filter by assigned agent (agent_owner)
        if agent_owner:
            todos = [t for t in todos if t.get("agent_owner") == agent_owner]

        # Filter by project tag
        if project:
            todos = [t for t in todos if t.get("project") == project]

        # Filter by search query (search in short_desc)
        if search:
            search_lower = search.lower()
            todos = [t for t in todos if search_lower in t.get("short_desc", "").lower()]

        # Sort
        if sort_by == "priority":
            todos = sorted(todos, key=lambda t: t["priority"], reverse=True)
        elif sort_by == "created":
            todos = sorted(todos, key=lambda t: t["created_at"], reverse=True)
        elif sort_by == "created_asc":
            todos = sorted(todos, key=lambda t: t["created_at"], reverse=False)
        elif sort_by == "updated":
            todos = sorted(todos, key=lambda t: t["updated_at"], reverse=True)
        elif sort_by == "updated_asc":
            todos = sorted(todos, key=lambda t: t["updated_at"], reverse=False)
        elif sort_by in ("completed", "completed_asc"):
            # Sort by completed_at in the requested direction; push todos with
            # completed_at=None to the end in BOTH directions (so the user
            # always sees finished work above unfinished work). (2026-06-05 per Arcurus.)
            reverse = sort_by == "completed"
            with_ca = [t for t in todos if t.get("completed_at")]
            without_ca = [t for t in todos if not t.get("completed_at")]
            with_ca.sort(key=lambda t: t["completed_at"], reverse=reverse)
            todos = with_ca + without_ca

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
            **kwargs: Fields to update. Valid fields: short_desc, long_desc, priority, status, sensitive, parent_id, estimated_llm_calls, creator_id, conversation_id, agent_id, project, agent_owner, what_happened, block_reason, waiting_for

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

    def _apply_completed_at_rule(self, todo: dict, new_status, explicit_completed_at, now: str):
        """
        Apply the auto-rule for `completed_at` based on status transitions.

        Rules (per Arcurus 2026-06-05):
        - status -> "completed": set completed_at = now
        - status -> "done": set completed_at = now only if currently null (preserve first
          completion time when a reviewer promotes a "completed" todo to "done")
        - status -> anything else (open, in_progress, blocked): set completed_at = null
        - If the caller passes `completed_at` explicitly, that value always wins
          (escape hatch for backfills / overrides).

        If `new_status` is None, no status change was requested in this update,
        so the auto-rule does not run.
        `explicit_completed_at` is the sentinel _SENTINEL_UNSET when the caller
        did not pass the field at all.
        """
        if new_status is None:
            return

        # Caller-supplied value (including None) wins over the auto-rule.
        if explicit_completed_at is not _SENTINEL_UNSET:
            return

        if new_status == "completed":
            todo["completed_at"] = now
        elif new_status == "done":
            if todo.get("completed_at") is None:
                todo["completed_at"] = now
            # else: keep existing completed_at (e.g. previously set when status=completed)
        else:
            # open / in_progress / blocked / unknown — clear it
            todo["completed_at"] = None

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

        # Pop completed_at up front so the auto-rule below can detect whether
        # the caller supplied an explicit value (including None) — in which
        # case it always wins. A bare empty string from the query parser is
        # treated the same as "not passed" so callers can leave the field off
        # cleanly.
        if "completed_at" in kwargs and kwargs["completed_at"] != "":
            explicit_completed_at = kwargs.pop("completed_at")
        else:
            kwargs.pop("completed_at", None)
            explicit_completed_at = _SENTINEL_UNSET

        new_status = kwargs.get("status")  # may be None (no status change)
        now = self._now()

        # Run the auto-rule on the pre-update todo so it sees the existing
        # completed_at (not anything we are about to overwrite). No-op when
        # the caller supplied an explicit value.
        self._apply_completed_at_rule(todo, new_status, explicit_completed_at, now)

        # Update allowed fields
        allowed = ["short_desc", "long_desc", "priority", "status", "sensitive", "parent_id", "estimated_llm_calls", "creator_id", "conversation_id", "agent_id", "project", "agent_owner", "what_happened", "block_reason", "waiting_for", "deleted_at"]
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

        # Apply the explicit completed_at last so it always wins over the auto-rule.
        if explicit_completed_at is not _SENTINEL_UNSET:
            todo["completed_at"] = explicit_completed_at

        todo["updated_at"] = self._now()

        # Save appropriate list
        if todo in self.todos:
            self._save_todos()
        else:
            self._save_sensitive_todos()

        return todo

    def delete_todo(self, todo_id: str, delete_children: bool = True) -> bool:
        """
        Soft delete a todo by ID (sets deleted_at timestamp). Returns True if deleted, False if not found.

        Args:
            todo_id: ID of todo to delete
            delete_children: If True, also soft-delete all child todos

        Returns:
            True if found and marked as deleted, False if not found
        """
        now = self._now()

        # Try regular todos first
        for todo in self.todos:
            if todo["id"] == todo_id:
                todo["deleted_at"] = now
                todo["updated_at"] = now
                if delete_children:
                    self._soft_delete_children(todo_id, self.todos, now)
                self._save_todos()
                return True

        # Try sensitive todos
        for todo in self.sensitive_todos:
            if todo["id"] == todo_id:
                todo["deleted_at"] = now
                todo["updated_at"] = now
                if delete_children:
                    self._soft_delete_children(todo_id, self.sensitive_todos, now)
                self._save_sensitive_todos()
                return True

        return False

    def _soft_delete_children(self, parent_id: str, todo_list: list, deleted_at: str):
        """Soft delete all children of a parent todo from a specific list."""
        children = [t for t in todo_list if t.get("parent_id") == parent_id]
        for child in children:
            # Recursively delete grandchildren
            self._soft_delete_children(child["id"], todo_list, deleted_at)
            child["deleted_at"] = deleted_at
            child["updated_at"] = deleted_at

    def purge_old_deleted(self, days: int = 7) -> int:
        """
        Permanently delete todos that have been soft-deleted more than `days` ago.

        Args:
            days: Number of days after which to purge deleted todos (default: 7)

        Returns:
            Number of todos purged
        """
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        purged_count = 0

        # Purge from regular todos
        todos_to_remove = []
        for todo in self.todos:
            if todo.get("deleted_at") and todo["deleted_at"] < cutoff_str:
                todos_to_remove.append(todo)
        for todo in todos_to_remove:
            self.todos.remove(todo)
            purged_count += 1
        if todos_to_remove:
            self._save_todos()

        # Purge from sensitive todos
        sensitive_to_remove = []
        for todo in self.sensitive_todos:
            if todo.get("deleted_at") and todo["deleted_at"] < cutoff_str:
                sensitive_to_remove.append(todo)
        for todo in sensitive_to_remove:
            self.sensitive_todos.remove(todo)
            purged_count += 1
        if sensitive_to_remove:
            self._save_sensitive_todos()

        return purged_count

    def _delete_children(self, parent_id: str, todo_list: list):
        """Delete all children of a parent todo from a specific list."""
        children = [t for t in todo_list if t.get("parent_id") == parent_id]
        for child in children:
            # Recursively delete grandchildren
            self._delete_children(child["id"], todo_list)
            todo_list.remove(child)

    def mark_done(self, todo_id: str, what_happened: Optional[str] = None) -> Optional[dict]:
        """Mark a todo as done. If what_happened is provided, also store it."""
        if what_happened is not None:
            return self.update_todo(todo_id, status="done", what_happened=what_happened)
        return self.update_todo(todo_id, status="done")

    def mark_in_progress(self, todo_id: str, what_happened: Optional[str] = None) -> Optional[dict]:
        """Mark a todo as in progress. If what_happened is provided, also store it (rare)."""
        if what_happened is not None:
            return self.update_todo(todo_id, status="in_progress", what_happened=what_happened)
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

    def get_filter_options(self, sensitive: Optional[bool] = None,
                          include_deleted: bool = False) -> dict:
        """
        Return distinct values for filterable fields (agent_owner, project)
        with counts, so the web UI can populate filter dropdowns.

        Args:
            sensitive: If None, all. If True, only sensitive. If False, only non-sensitive.
            include_deleted: If True, include soft-deleted todos when computing options.

        Returns:
            {
              "agent_owners": [{"value": "selena-project-worker", "count": 42}, ...],
              "projects":    [{"value": "selena-project", "count": 17}, ...]
            }
        """
        if sensitive is None:
            all_todos = self._get_all_todos()
        elif sensitive:
            all_todos = self._get_todo_list(True)
        else:
            all_todos = self._get_todo_list(False)

        if not include_deleted:
            all_todos = [t for t in all_todos if not t.get("deleted_at")]

        agent_counts: dict = {}
        project_counts: dict = {}
        for t in all_todos:
            owner = t.get("agent_owner")
            if owner:
                agent_counts[owner] = agent_counts.get(owner, 0) + 1
            proj = t.get("project")
            if proj:
                project_counts[proj] = project_counts.get(proj, 0) + 1

        # Sort by count desc, then by name asc (stable, predictable order)
        agent_owners = [
            {"value": k, "count": v}
            for k, v in sorted(agent_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        projects = [
            {"value": k, "count": v}
            for k, v in sorted(project_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return {"agent_owners": agent_owners, "projects": projects}


# Global instance
todo_manager = TodoManager()
