# Selena v2 - API Documentation

*Last Updated: 2026-04-19*

## Base URL
```
http://localhost:8765
```

## Authentication
All endpoints (except `/api/login`) require Bearer token authentication.
Include the token in the `Authorization` header:
```
Authorization: Bearer YOUR_TOKEN_HERE
```

---

## Authentication

### Login
**POST** `/api/login?password=PASSWORD`

Login to get an auth token.

**Response:**
```json
{
  "success": true,
  "token": "abc123..."
}
```

---

## LLM Call Tracking

### Get LLM Call Status
**GET** `/api/llm-calls`

Get current LLM call usage and limit.

**Response:**
```json
{
  "used": 42,
  "limit": 4000,
  "remaining": 3958,
  "usage_percent": 1.1,
  "reset_info": "Token plan refreshes every 5 hours"
}
```

---

## Todo / Loose Ends Management

### Get All Todos
**GET** `/api/todos?status=open&sort_by=priority`

Get all todos, optionally filtered by status and sorted.

**Parameters:**
- `status` (optional): Filter by status - `open`, `in_progress`, `done`
- `sort_by` (optional): Sort by - `priority` (default), `created`, `updated`

**Response:**
```json
{
  "todos": [
    {
      "id": "a5eda4d6",
      "short_desc": "Build todo web interface",
      "long_desc": "Display loose ends in web interface...",
      "priority": 9,
      "status": "open",
      "sensitive": false,
      "parent_id": null,
      "estimated_llm_calls": 10,
      "creator_id": "main",
      "conversation_id": "channel:1495170712397152367",
      "agent_id": "selena-v2",
      "created_at": "2026-04-19T00:05:56.318686",
      "updated_at": "2026-04-19T00:05:56.318698"
    }
  ],
  "summary": {
    "total": 5,
    "open": 4,
    "in_progress": 0,
    "done": 1,
    "total_llm_calls": 50,
    "open_llm_calls": 30,
    "top_priority": [...]
  }
}
```

### Get Todo Summary
**GET** `/api/todos/summary?sensitive=true|false`

Get a quick summary of all todos.

**Parameters:**
- `sensitive` (optional): Filter by sensitive status - `true` or `false`

**Response:**
```json
{
  "total": 5,
  "open": 4,
  "in_progress": 0,
  "done": 1,
  "total_llm_calls": 50,
  "open_llm_calls": 30,
  "top_priority": [...]
}
```

### Add Todo
**POST** `/api/todos/add?short_desc=TITLE&long_desc=DESCRIPTION&priority=9&sensitive=false&parent_id=ID&estimated_llm_calls=10&creator_id=ID&conversation_id=ID&agent_id=ID`

Add a new todo.

**Parameters:**
- `short_desc` (required): Brief title (1-200 chars)
- `long_desc` (optional): Detailed description
- `priority` (optional): 1-10, 10 is highest (default: 5)
- `sensitive` (optional): If `true`, stored in `todos.env` NOT in git (default: false)
- `parent_id` (optional): Parent todo ID for hierarchical todos
- `estimated_llm_calls` (optional): Estimated LLM calls for this task
- `creator_id` (optional): ID of who created this todo
- `conversation_id` (optional): ID of the conversation this belongs to
- `agent_id` (optional): ID of the agent that owns this todo

**Response:**
```json
{
  "success": true,
  "todo": {
    "id": "abc123",
    "short_desc": "Build something",
    "long_desc": "Detailed description",
    "priority": 9,
    "status": "open",
    "sensitive": false,
    "parent_id": null,
    "estimated_llm_calls": 10,
    "creator_id": "main",
    "conversation_id": "channel:1495170712397152367",
    "agent_id": "selena-v2",
    "created_at": "2026-04-19T00:00:00.000000",
    "updated_at": "2026-04-19T00:00:00.000000"
  }
}
```

### Update Todo
**POST** `/api/todos/update?id=ID&short_desc=TITLE&long_desc=DESCRIPTION&priority=9&status=status&sensitive=true&parent_id=ID&estimated_llm_calls=20&creator_id=ID&conversation_id=ID&agent_id=ID`

Update an existing todo.

**Parameters:**
- `id` (required): Todo ID
- `short_desc` (optional): New title
- `long_desc` (optional): New description
- `priority` (optional): New priority (1-10)
- `status` (optional): New status (`open`, `in_progress`, `done`)
- `sensitive` (optional): If `true`, moves to `todos.env`
- `parent_id` (optional): New parent ID (use empty to remove hierarchy)
- `estimated_llm_calls` (optional): Updated estimate
- `creator_id` (optional): Updated creator ID
- `conversation_id` (optional): Updated conversation ID
- `agent_id` (optional): Updated agent ID

**Response:**
```json
{
  "success": true,
  "todo": { ... updated todo ... }
}
```

### Get Children
**GET** `/api/todos/children?parent_id=ID`

Get all child todos of a parent todo.

**Parameters:**
- `parent_id` (required): Parent todo ID

**Response:**
```json
{
  "children": [
    {
      "id": "child123",
      "short_desc": "Subtask 1",
      "parent_id": "parent123",
      ...
    }
  ]
}
```

### Split Todo
**POST** `/api/todos/split?id=ID&subtasks=TASK1|||TASK2|||TASK3`

Split a big todo into smaller subtasks.

**Parameters:**
- `id` (required): Parent todo ID to split
- `subtasks` (required): Subtask titles separated by `|||` (or comma which auto-converts)

**Response:**
```json
{
  "success": true,
  "subtasks": [
    { "id": "sub1", "short_desc": "Subtask 1", "parent_id": "parent123", ... },
    { "id": "sub2", "short_desc": "Subtask 2", "parent_id": "parent123", ... }
  ]
}
```

### Mark Todo as Done
**POST** `/api/todos/mark-done?id=ID`

Mark a todo as done.

**Response:**
```json
{
  "success": true,
  "todo": { ... updated todo ... }
}
```

### Delete Todo
**POST** `/api/todos/delete?id=ID`

Delete a todo (and optionally all its children).

**Response:**
```json
{
  "success": true
}
```

---

## Priority System

### Get Priority Tasks
**GET** `/api/priority/tasks`

Get all priority tasks.

### Get Top Priority
**GET** `/api/priority/top`

Get the top priority task.

### Add Priority Task
**POST** `/api/priority/add?name=TASK_NAME&description=DESCRIPTION&impact=9&urgency=7&effort=5&dependencies=3&learning=8&joy=7`

Add a new priority task with scores (1-10 each).

**Parameters:**
- `name` (required): Task name
- `description` (optional): Task description
- `impact` (optional): Impact score 1-10 (default: 5)
- `urgency` (optional): Urgency score 1-10 (default: 5)
- `effort` (optional): Effort score 1-10 (default: 5)
- `dependencies` (optional): Dependencies score 1-10 (default: 5)
- `learning` (optional): Learning score 1-10 (default: 5)
- `joy` (optional): Joy score 1-10 (default: 5)

### Clear All Tasks
**POST** `/api/priority/clear`

Clear all priority tasks.

---

## Knowledge Base

Knowledge base for lessons learned, skills, patterns, and references.

**Categories:** `lessons`, `skills`, `patterns`, `references`

### Get All Knowledge Entries
**GET** `/api/knowledge?category=lessons&search=error`

Get all knowledge entries, optionally filtered by category or search term.

**Parameters:**
- `category` (optional): Filter by category - `lessons`, `skills`, `patterns`, `references`
- `search` (optional): Search in title, content, and tags

**Response:**
```json
{
  "entries": [
    {
      "id": "abc123",
      "category": "lessons",
      "title": "Handle API errors gracefully",
      "content": "Always check return values and handle...",
      "tags": ["api", "error-handling"],
      "created_at": "2026-04-19T00:00:00",
      "updated_at": "2026-04-19T00:00:00"
    }
  ],
  "categories": {
    "lessons": 5,
    "skills": 3,
    "patterns": 2,
    "references": 1
  }
}
```

### Get Categories
**GET** `/api/knowledge/categories`

Get category counts.

**Response:**
```json
{
  "categories": {
    "lessons": 5,
    "skills": 3,
    "patterns": 2,
    "references": 1
  }
}
```

### Add Knowledge Entry
**POST** `/api/knowledge/add?category=lessons&title=TITLE&content=CONTENT&tags=tag1,tag2`

Add a new knowledge entry.

**Parameters:**
- `category` (required): Category - `lessons`, `skills`, `patterns`, `references`
- `title` (required): Entry title (1-200 chars)
- `content` (optional): Detailed content
- `tags` (optional): Comma-separated tags

**Response:**
```json
{
  "success": true,
  "entry": {
    "id": "abc123",
    "category": "lessons",
    "title": "Handle API errors gracefully",
    "content": "Always check return values...",
    "tags": ["api", "error-handling"],
    "created_at": "2026-04-19T00:00:00",
    "updated_at": "2026-04-19T00:00:00"
  }
}
```

### Update Knowledge Entry
**POST** `/api/knowledge/update?id=ID&title=NEW_TITLE&content=NEW_CONTENT`

Update an existing knowledge entry.

**Parameters:**
- `id` (required): Entry ID
- `title` (optional): New title
- `content` (optional): New content
- `category` (optional): New category
- `tags` (optional): Comma-separated new tags

**Response:**
```json
{
  "success": true,
  "entry": { ... updated entry ... }
}
```

### Delete Knowledge Entry
**POST** `/api/knowledge/delete?id=ID`

Delete a knowledge entry.

**Response:**
```json
{
  "success": true
}
```

---

## Self-Evolution Loop

### Get Evolution Status
**GET** `/api/evolution/status`

Get the status of the self-evolution loop.

**Response:**
```json
{
  "running": true,
  "interval_minutes": 10,
  "evolution_count": 5,
  "last_evolution": "Improved memory system",
  "system_health": {
    "api_server": true,
    "scheduler": true,
    "web_interface": true,
    "memory": true
  },
  "identified_improvements": [...],
  "recent_log": [...]
}
```

### Start Evolution Loop
**POST** `/api/evolution/start`

Start the self-evolution loop.

### Stop Evolution Loop
**POST** `/api/evolution/stop`

Stop the self-evolution loop.

### Trigger Evolution
**POST** `/api/evolution/trigger`

Trigger one evolution cycle immediately.

### Check System Health
**GET** `/api/evolution/health`

Check system health through the evolution loop.

---

## World Scheduler

### Get Scheduler Status
**GET** `/api/world/scheduler/status`

Get the status of the world scheduler.

**Response:**
```json
{
  "running": true,
  "interval_seconds": 30,
  "actions_per_cycle": 3,
  "last_action_time": "2026-04-19 00:00:00",
  "last_entity": "Shadow Crown",
  "last_outcome": "The Shadow Crown pulses with dark energy...",
  "action_count": 42,
  "error_count": 0,
  "world_name": "The realm of Shadows",
  "entity_count": 12,
  "world_action_count": 126,
  "open_world_url": "http://localhost:8080",
  "recent_log": [...]
}
```

### Start Scheduler
**POST** `/api/world/scheduler/start`

Start the world scheduler.

### Stop Scheduler
**POST** `/api/world/scheduler/stop`

Stop the world scheduler.

---

## Knowledge Base

The Knowledge Base stores lessons, skills, patterns, and reference information.

### Get Knowledge Entries
**GET** `/api/knowledge?category=lesson&search=keyword`

Get all knowledge entries, optionally filtered.

**Parameters:**
- `category` (optional): Filter by category - `lesson`, `skill`, `pattern`, `reference`
- `search` (optional): Search in title and content

**Response:**
```json
{
  "entries": [
    {
      "id": "abc123",
      "category": "lesson",
      "title": "Always check the API first",
      "content": "Before implementing, always check what the API expects...",
      "tags": ["api", "workflow"],
      "created_at": "2026-04-19T00:00:00.000000"
    }
  ],
  "categories": [
    {"name": "lesson", "count": 5},
    {"name": "skill", "count": 3},
    {"name": "pattern", "count": 2},
    {"name": "reference", "count": 1}
  ]
}
```

### Get Categories
**GET** `/api/knowledge/categories`

Get all knowledge categories with entry counts.

**Response:**
```json
{
  "categories": [
    {"name": "lesson", "count": 5},
    {"name": "skill", "count": 3},
    {"name": "pattern", "count": 2},
    {"name": "reference", "count": 1}
  ]
}
```

### Add Knowledge Entry
**POST** `/api/knowledge/add?category=lesson&title=TITLE&content=CONTENT&tags=tag1,tag2`

Add a new knowledge entry.

**Parameters:**
- `category` (required): Category - `lesson`, `skill`, `pattern`, `reference`
- `title` (required): Brief title
- `content` (required): The knowledge content
- `tags` (optional): Comma-separated tags

**Response:**
```json
{
  "success": true,
  "entry": {
    "id": "abc123",
    "category": "lesson",
    "title": "Test Lesson",
    "content": "This is a test lesson...",
    "tags": ["test"],
    "created_at": "2026-04-19T00:00:00.000000"
  }
}
```

### Update Knowledge Entry
**POST** `/api/knowledge/update?id=ID&title=TITLE&content=CONTENT&tags=tag1,tag2&category=lesson`

Update an existing knowledge entry.

**Parameters:**
- `id` (required): Entry ID
- `title` (optional): New title
- `content` (optional): New content
- `tags` (optional): New tags (comma-separated)
- `category` (optional): Move to different category

**Response:**
```json
{
  "success": true,
  "entry": { ... updated entry ... }
}
```

### Delete Knowledge Entry
**POST** `/api/knowledge/delete?id=ID`

Delete a knowledge entry.

**Parameters:**
- `id` (required): Entry ID

**Response:**
```json
{
  "success": true
}
```

---

## System Status

### Get System Status
**GET** `/api/status`

Get overall system status including all running services.

---

## Notes

- All timestamps are in ISO 8601 format
- All responses are JSON
- Error responses include an `error` field with error message
- The web interface is available at `http://localhost:8765/`
- Todos are persisted to `~/openclaw/workspace/selena/data/todos.json`
- LLM call tracking is stored in `~/openclaw/workspace/selena/data/llm_calls.json`
