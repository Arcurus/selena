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
      "created_at": "2026-04-19T00:05:56.318686",
      "updated_at": "2026-04-19T00:05:56.318698"
    }
  ],
  "summary": {
    "total": 5,
    "open": 4,
    "in_progress": 0,
    "done": 1,
    "top_priority": [...]
  }
}
```

### Get Todo Summary
**GET** `/api/todos/summary`

Get a quick summary of all todos.

**Response:**
```json
{
  "total": 5,
  "open": 4,
  "in_progress": 0,
  "done": 1,
  "top_priority": [...]
}
```

### Add Todo
**POST** `/api/todos/add?short_desc=TITLE&long_desc=DESCRIPTION&priority=9`

Add a new todo.

**Parameters:**
- `short_desc` (required): Brief title (1-200 chars)
- `long_desc` (optional): Detailed description
- `priority` (optional): 1-10, 10 is highest (default: 5)

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
    "created_at": "2026-04-19T00:00:00.000000",
    "updated_at": "2026-04-19T00:00:00.000000"
  }
}
```

### Update Todo
**POST** `/api/todos/update?id=ID&short_desc=TITLE&long_desc=DESCRIPTION&priority=9&status=status`

Update an existing todo.

**Parameters:**
- `id` (required): Todo ID
- `short_desc` (optional): New title
- `long_desc` (optional): New description
- `priority` (optional): New priority (1-10)
- `status` (optional): New status (`open`, `in_progress`, `done`)

**Response:**
```json
{
  "success": true,
  "todo": { ... updated todo ... }
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

Delete a todo.

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
