#!/usr/bin/env python3
"""
Selena v2 - API Server
======================

Simple API server with password protection for the Selena web interface.

Usage:
    python3 api_server.py
"""

import os
import sys
import json
import argparse
import hashlib
import datetime
from datetime import timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# Load environment variables from .env
def load_env():
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if '=' in line:
                        key, value = line.split('=', 1)
                        os.environ[key] = value

load_env()

# Import the world scheduler and priority reflector
import sys
sys.path.insert(0, os.path.dirname(__file__))
from priority_reflector import reflector, PriorityTask, PriorityReflector
from self_evolution import evolution_loop
from llm_call_tracker import get_tracker as _get_llm_tracker
from cost_tracker import build_daily_report as _ct_build_daily, build_weekly_report as _ct_build_weekly_report, render_markdown as _ct_render, post_to_discord as _ct_post
# OpenClaw session-usage tracker (proper input/output split, all sessions).
# Lives in code/openclaw_cost_tracker.py; this import fails soft so the rest of
# the API server keeps working if the file isn't there yet.
try:
    import openclaw_usage as _openclaw_usage
except Exception as _imp_err:  # noqa: BLE001
    _openclaw_usage = None
    _OPENCLAW_USAGE_IMPORT_ERROR = str(_imp_err)
else:
    _OPENCLAW_USAGE_IMPORT_ERROR = None
from todo_manager import todo_manager, TodoDuplicateError
from knowledge_base import knowledge_base as kb
from workspace_scanner import scanner, scan_workspace, get_last_scan, get_scan_history
from service_manager import (
    monitor as service_monitor,
    load_services as sm_load_services,
    get_service as sm_get_service,
    is_monitored as sm_is_monitored,
    check_and_restart_cycle,
    update_service as sm_update_service,
    check_health as sm_check_health,
    load_state as sm_load_state,
    load_restart_history as sm_load_restart_history,
)
from discord_client import DiscordNotifier, get_default as get_default_notifier, post_to_default
from cron_tracker import (
    list_jobs as _cron_list_jobs,
    get_job_summary as _cron_get_summary,
    enable_job as _cron_enable,
    disable_job as _cron_disable,
    set_model as _cron_set_model,
    set_context as _cron_set_context,
    get_instructions as _cron_get_instructions,
)

# Helper functions for service management
import time
import signal
import subprocess

# Module load timestamp — used by /api/health to report uptime
API_START_TS = time.time()

# -----------------------------------------------------------------------------
# MiniMax usage cache (10-second TTL, per Arcurus 2026-06-11 #lunar-project:
# "5 min for minimax budget update seems quite long, better use 10 secs")
# Used by the Moderation → Last Run sub-tab widget so the LLM usage bar
# doesn't hammer the MiniMax token plan API on every 10s poll.
# -----------------------------------------------------------------------------
_MINIMAX_CACHE = {"ts": 0.0, "payload": None}
_MINIMAX_TTL_SECONDS = 10

def _minimax_cache_get():
    if _MINIMAX_CACHE["payload"] is None:
        return None
    if (time.time() - _MINIMAX_CACHE["ts"]) > _MINIMAX_TTL_SECONDS:
        return None
    out = dict(_MINIMAX_CACHE["payload"])
    out["cached"] = True
    return out

def _minimax_cache_set(payload):
    _MINIMAX_CACHE["ts"] = time.time()
    _MINIMAX_CACHE["payload"] = payload

# -----------------------------------------------------------------------------
# Live MiniMax quota fetcher (added 2026-06-10 per Arcurus #cost-tracker,
# TTL changed 2026-06-11 per Arcurus #lunar-project: "5 min for minimax
# budget update seems quite long, better use 10 secs").
#
# The web widget's "MiniMax 5h" card was reading a STALE field in the
# snapshot (`providers.minimax.quota.models.*.window_5h.remaining_percent`)
# that hadn't been refreshed since 2026-06-08 (when the direct
# `/v1/token_plan/remains` API call last worked). The snapshot also has a
# fresh `minimax_interval` field, but to guarantee the widget always shows
# the most recent value, we now call `mmx quota` directly inside the
# endpoint. The result is cached for 10 seconds by `_minimax_cache_get` so
# the 10-second client poll never hammers the upstream.
#
# This deliberately mirrors the parsing in code/budget_gate.py
# (`_parse_mmx_quota`) and code/cost_tracker.py (`_format_mmx_section`)
# but keeps it inline and never raises — on any failure (binary missing,
# timeout, non-zero exit, non-JSON output) it returns `ok: False` and
# the caller falls back to whatever's in the snapshot.
# -----------------------------------------------------------------------------
_MMX_LIVE_TIMEOUT_S = 10

def _live_mmx_quota() -> dict:
    """Call `mmx quota` and return a parsed dict, or `{ok: False, error: ...}`.

    Never raises. On failure, the caller should fall back to the snapshot.
    """
    import subprocess
    import shutil
    from datetime import datetime as _dt, timezone as _tz
    out: dict = {
        "ok": False,
        "source": "mmx-cli",
        "fetched_at": _dt.now(_tz.utc).isoformat(),
    }
    # Resolve the `mmx` binary: try the same candidates as budget_gate.
    mmx_candidates = [
        os.environ.get("MMX_BIN"),
        os.path.expanduser("~/.npm-global/bin/mmx"),
        "/home/openclaw/.npm-global/bin/mmx",
        shutil.which("mmx"),
    ]
    mmx_path = next((c for c in mmx_candidates if c and (c == "mmx" or os.path.isfile(c))), None)
    if not mmx_path:
        out["error"] = "mmx CLI not found"
        return out
    try:
        proc = subprocess.run(
            [mmx_path, "quota"],
            capture_output=True, text=True, timeout=_MMX_LIVE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        out["error"] = f"mmx quota timed out after {_MMX_LIVE_TIMEOUT_S}s"
        return out
    except FileNotFoundError:
        out["error"] = f"mmx CLI not found at {mmx_path}"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"mmx quota error: {type(e).__name__}: {e}"
        return out
    if proc.returncode != 0:
        out["error"] = f"mmx exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
        return out
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        out["error"] = f"mmx quota returned non-JSON: {e}"
        out["raw"] = (proc.stdout or "")[:400]
        return out
    out["ok"] = True
    out["data"] = data
    return out

# -----------------------------------------------------------------------------
# Project definitions cache (added 2026-06-09 per Arcurus #cost-tracker).
# Source of truth = data/project_mapping.json. Used by /api/llm-usage
# to roll up child project spend into their parents (e.g. open-world-dev
# + open-world-running -> open-world-selena). Reloaded on every call
# (cheap; <1ms) so editing the JSONL is picked up without a restart.
# -----------------------------------------------------------------------------
_PROJECT_DEFS_CACHE: dict = {"ts": 0.0, "projects": {}}
_PROJECT_DEFS_TTL_SECONDS = 5

def _load_project_defs() -> dict:
    """Return the parsed `projects` map from data/project_mapping.json.

    Schema: slug -> { title, emoji, description, color, parentProject?,
                       worker_cron_id?, primary_channel_id?, ... }
    Cached for 5s to avoid hitting disk on every API call.
    """
    now = time.time()
    if now - _PROJECT_DEFS_CACHE["ts"] < _PROJECT_DEFS_TTL_SECONDS:
        return _PROJECT_DEFS_CACHE["projects"]
    try:
        with open(os.path.join(SELENA_ROOT, "data", "project_mapping.json"), encoding="utf-8") as f:
            data = json.load(f)
        _PROJECT_DEFS_CACHE["projects"] = data.get("projects", {}) or {}
    except (OSError, json.JSONDecodeError):
        _PROJECT_DEFS_CACHE["projects"] = {}
    _PROJECT_DEFS_CACHE["ts"] = now
    return _PROJECT_DEFS_CACHE["projects"]


# -----------------------------------------------------------------------------
# OpenClaw gateway config cache (added 2026-06-04 per lunar todo 8b635506)
# The /v1/chat/completions proxy needs the gateway's URL and bearer password.
# Reading the config on every call would be wasteful, so we cache it.
# -----------------------------------------------------------------------------
_OPENCLAW_GATEWAY_CACHE = {"ts": 0.0, "url": None, "password": None}
_OPENCLAW_GATEWAY_TTL_SECONDS = 300

def _get_openclaw_gateway():
    """Return (url, password) for the OpenClaw gateway, or (url, None) if
    the password is not in the config (the endpoint will return 503).
    Cached for 5 minutes."""
    now = time.time()
    if (_OPENCLAW_GATEWAY_CACHE["url"] is not None
            and (now - _OPENCLAW_GATEWAY_CACHE["ts"]) < _OPENCLAW_GATEWAY_TTL_SECONDS):
        return _OPENCLAW_GATEWAY_CACHE["url"], _OPENCLAW_GATEWAY_CACHE["password"]
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    url, pw = "http://localhost:18789", None
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        gw = cfg.get("gateway", {}) or {}
        port = gw.get("port", 18789)
        url = f"http://localhost:{port}"
        auth = gw.get("auth", {}) or {}
        pw = auth.get("password")
    except Exception:
        pass
    _OPENCLAW_GATEWAY_CACHE["ts"] = now
    _OPENCLAW_GATEWAY_CACHE["url"] = url
    _OPENCLAW_GATEWAY_CACHE["password"] = pw
    return url, pw

def get_pid_file(pid_path):
    """Read PID from a file, return None if not found"""
    if os.path.exists(pid_path):
        try:
            with open(pid_path, 'r') as f:
                return int(f.read().strip())
        except:
            return None
    return None

def check_process_running(pid):
    """Check if a process with given PID is running"""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except:
        return False

def get_pid_for_command(cmd1, cmd2=None):
    """Find PID for a running command using ps"""
    try:
        if cmd2:
            result = subprocess.run(['pgrep', '-f', cmd1], capture_output=True, text=True)
            for line in result.stdout.strip().split('\n'):
                if line:
                    pid = int(line)
                    try:
                        with open(f'/proc/{pid}/cmdline', 'r') as f:
                            cmdline = f.read()
                            if cmd2 in cmdline:
                                return pid
                    except:
                        pass
        else:
            result = subprocess.run(['pgrep', '-f', cmd1], capture_output=True, text=True)
            pids = result.stdout.strip().split('\n')
            if pids and pids[0]:
                return int(pids[0])
    except:
        pass
    return None

# Configuration
PORT = int(os.getenv('SELENA_PORT', '8765'))
WEB_PASSWORD = os.getenv('WEB_PASSWORD', 'change_me')
API_PASSWORD = os.getenv('WEB_PASSWORD', 'change_me')
# Static service token for service-to-service /api/llm-usage/record calls
# (Open World Rust server, scheduled_actions.py, etc.).  Long-lived so the
# service can keep a single token in its env.  If unset, the record
# endpoint falls back to user-bearer auth only.
LLM_RECORD_TOKEN = os.getenv('LLM_RECORD_TOKEN', '').strip()

# Per-agent default channels for /api/discord/send (added 2026-06-06 per
# Arcurus todo 395bb4b0).  When a caller (e.g. the slow-heartbeat cron)
# omits the `channel` query param, the API falls back to these instead
# of the notifier's #selena-project default.  This keeps silent-misroute
# posts out of #selena-project and routes them to the right lane.
# Override via env: AGENT_DEFAULT_CHANNELS="agent1=chan1,agent2=chan2".
AGENT_DEFAULT_CHANNELS = {
    'slow-heartbeat': '1494781163498246144',  # #heartbeats
}
_env_agent_channels = os.getenv('AGENT_DEFAULT_CHANNELS', '').strip()
if _env_agent_channels:
    for pair in _env_agent_channels.split(','):
        if '=' in pair:
            k, v = pair.split('=', 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                AGENT_DEFAULT_CHANNELS[k] = v
SELENA_ROOT = os.path.expanduser('~/openclaw/workspace/selena-project')
DATA_DIR = os.path.join(SELENA_ROOT, 'data')

# Simple auth token storage
active_tokens = {}

# LLM call tracking
llm_call_count = 0
llm_call_limit = 4000  # 4000 calls per 5 hours

def track_llm_call():
    """Track an LLM call."""
    global llm_call_count
    llm_call_count += 1
    return llm_call_count

# Activity log for tracking API requests and events
activity_log = []
MAX_ACTIVITY_LOG = 100

def log_activity(message, log_type='info'):
    """Log an activity event."""
    global activity_log
    timestamp = datetime.datetime.now().isoformat()
    activity_log.append({
        'time': timestamp,
        'message': message,
        'type': log_type
    })
    # Keep only the last MAX_ACTIVITY_LOG entries
    if len(activity_log) > MAX_ACTIVITY_LOG:
        activity_log = activity_log[-MAX_ACTIVITY_LOG:]


# Error logging (daily rotating files)
def get_error_log_path():
    """Get path to today's error log file."""
    log_dir = os.path.join(os.path.dirname(__file__), '..', 'log')
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    return os.path.join(log_dir, f'error-log-{today}.log')

def log_error(error_msg, context=''):
    """Log an error to the daily error log file."""
    timestamp = datetime.datetime.now().isoformat()
    log_line = f"[{timestamp}] ERROR: {error_msg}"
    if context:
        log_line += f" | Context: {context}"
    log_line += "\n"
    try:
        with open(get_error_log_path(), 'a') as f:
            f.write(log_line)
    except Exception as e:
        # Fallback to stderr if file logging fails
        import sys
        print(f"Failed to write to error log: {e}", file=sys.stderr)


# API request logging (daily rotating files)
def get_api_log_path():
    """Get path to today's API log file."""
    log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    return os.path.join(log_dir, f'api-server-{today}.log')

def log_api(action, details=''):
    """Log an API request to the daily API log file."""
    timestamp = datetime.datetime.now().isoformat()
    log_line = f"[{timestamp}] API: {action}"
    if details:
        log_line += f" | Details: {details}"
    log_line += "\n"
    try:
        with open(get_api_log_path(), 'a') as f:
            f.write(log_line)
    except Exception as e:
        # Fallback to stderr if file logging fails
        import sys
        print(f"Failed to write to API log: {e}", file=sys.stderr)


def generate_token():
    """Generate a simple auth token"""
    import secrets
    return secrets.token_hex(16)


def hash_password(password):
    """Simple password hashing"""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password):
    """Verify password against stored hash"""
    return hash_password(password) == hash_password(WEB_PASSWORD)


def get_files_recursive(directory, base_path=''):
    """Get all files in directory recursively"""
    files = []
    try:
        for item in os.listdir(directory):
            item_path = os.path.join(directory, item)
            rel_path = os.path.join(base_path, item)
            
            if os.path.isdir(item_path):
                # Skip hidden directories and common non-essential dirs
                if not item.startswith('.') and item not in ['__pycache__', 'node_modules', 'venv']:
                    files.extend(get_files_recursive(item_path, rel_path))
            else:
                # Get file stats
                stat = os.stat(item_path)
                files.append({
                    'path': rel_path,
                    'size': stat.st_size,
                    'modified': datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'type': 'directory' if os.path.isdir(item_path) else 'file'
                })
    except PermissionError:
        pass
    return files


def get_memory_structure():
    """Get memory structure"""
    memory_dir = os.path.join(SELENA_ROOT, 'memory')
    if not os.path.exists(memory_dir):
        return []
    
    structure = []
    for root, dirs, files in os.walk(memory_dir):
        # Skip hidden dirs
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for file in files:
            if file.endswith('.md'):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, SELENA_ROOT)
                stat = os.stat(file_path)
                
                # Read first few lines for preview
                with open(file_path, 'r') as f:
                    lines = [f.readline().strip() for _ in range(3)]
                    preview = ' '.join([l for l in lines if l and not l.startswith('#')])[:100]
                
                structure.append({
                    'path': rel_path,
                    'name': file,
                    'preview': preview,
                    'modified': datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'size': stat.st_size
                })
    
    return structure


def get_agent_info():
    """Get information about agents/workers"""
    # Return mock agents for now
    return [
        {
            'id': 'selena_overseer',
            'name': 'Selena Overseer',
            'role': 'Main agent, coordinates all work',
            'status': 'active',
            'tasks_completed': 12,
            'current_task': 'Managing Open World development',
            'memory_usage': '45MB',
            'llm_calls': 156
        },
        {
            'id': 'shiba_miner',
            'name': 'Shiba Miner',
            'role': 'Mining context and memories',
            'status': 'working',
            'tasks_completed': 34,
            'current_task': 'Searching memory for relevant context',
            'memory_usage': '12MB',
            'llm_calls': 89
        },
        {
            'id': 'shiba_coder',
            'name': 'Shiba Coder',
            'role': 'Writing and updating code',
            'status': 'active',
            'tasks_completed': 23,
            'current_task': 'Updating Open World persistence',
            'memory_usage': '8MB',
            'llm_calls': 67
        }
    ]


def get_memory_relations():
    """Get vector relations between memory files (simplified)"""
    memories = get_memory_structure()
    relations = []
    
    # Create simplified relations based on path structure
    for mem in memories:
        path = mem['path']
        if 'daily' in path:
            relations.append({
                'from': path,
                'to': 'memory/global',
                'type': 'daily_to_global'
            })
        elif 'global' in path:
            relations.append({
                'from': path,
                'to': 'soul.md',
                'type': 'global_to_soul'
            })
    
    return relations


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler with password protection"""
    
    def log_message(self, format, *args):
        """Override to log requests"""
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {args[0]}")
    
    def send_cors_headers(self):
        """Send CORS headers"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
    
    def authenticate(self):
        """Check if request is authenticated via Authorization header or cookie"""
        # Check Authorization header first
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            return active_tokens.get(token, False)
        
        # Check cookie
        cookie_header = self.headers.get('Cookie', '')
        for cookie in cookie_header.split(';'):
            cookie = cookie.strip()
            if cookie.startswith('selena_token='):
                token = cookie[13:]  # len('selena_token=') == 13
                return active_tokens.get(token, False)
        
        return False
    
    def send_json(self, data, status=200):
        """Send JSON response"""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def send_html(self, html, status=200):
        """Send HTML response"""
        self.send_response(status)
        self.send_header('Content-Type', 'text/html')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(html.encode())

    def send_text(self, text, status=200, content_type='text/plain; charset=utf-8'):
        """Send plain-text response"""
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(text.encode('utf-8') if isinstance(text, str) else text)
    
    def do_OPTIONS(self):
        """Handle CORS preflight"""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests - delegates to _dispatch_routes for the
        method-agnostic route table. (Kept for BaseHTTPRequestHandler
        dispatch compatibility; the real work is in _dispatch_routes.)
        """
        parsed = urlparse(self.path)
        path = parsed.path
        self._dispatch_routes(path, 'GET')

    def _dispatch_routes(self, path, command):
        """Single route dispatch. Called from do_GET (and from the
        do_POST/do_PUT/do_PATCH/do_DELETE methods defined later in the
        class). The route table is checked in order; each block guards
        on `self.command == '<verb>'` so a POST route only matches POST
        requests, etc.

        Why this exists: prior to 2026-06-04 the pending-action POST/
        PATCH/DELETE routes had been copy-pasted into the do_GET method
        body by mistake, which meant they never fired for non-GET
        requests and the user saw "Clear failed: Unexpected token '<'"
        (the server was returning 501 with an HTML page from the default
        BaseHTTPRequestHandler handler). Consolidating the route table
        in one method eliminates that class of bug.
        """
        self.command = command  # route bodies use self.command for the verb check
        # Re-parse the URL so blocks that need the query string (parsed.query)
        # can use it. (Originally the do_GET method defined `parsed` once;
        # when the body moved into _dispatch_routes we have to do it here.)
        parsed = urlparse(self.path)
        # Serve images from web/images/ directory
        if path.startswith('/images/'):
            web_dir = os.path.join(SELENA_ROOT, 'web')
            # Remove leading slash and map to web/images/ directory
            web_dir = os.path.join(SELENA_ROOT, 'web')
            # Remove leading slash and map to web/images/ directory
            file_path = path.lstrip('/')
            full_path = os.path.join(web_dir, file_path)
            if os.path.exists(full_path) and os.path.isfile(full_path):
                # Determine content type
                ext = os.path.splitext(full_path)[1].lower()
                content_types = {
                    '.png': 'image/png',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.gif': 'image/gif',
                }
                content_type = content_types.get(ext, 'application/octet-stream')
                # HTML/JS/CSS change frequently and a stale cached copy
                # hides bug fixes (e.g. 2026-06-12: user saw the old
                # Per-Provider Quota card for 1+ hour after we shipped
                # the fix because index.html was cached for 3600s).
                # We use a short max-age + must-revalidate for HTML
                # (forces the browser to revalidate before reusing the
                # cached copy), and a long max-age for everything else
                # (CSS/JS/images don't change often and benefit from
                # the cache).  This is the standard pattern: short TTL
                # for the entry point, long TTL for sub-resources.
                if ext in ('.html', '.htm'):
                    cache_control = 'no-cache, must-revalidate'
                else:
                    cache_control = 'public, max-age=3600'
                try:
                    with open(full_path, 'rb') as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', len(content))
                    self.send_header('Cache-Control', cache_control)
                    # ETag based on file mtime so the browser can do a
                    # 304 Not Modified round-trip if the file hasn't
                    # changed. Cheap and correct.
                    import os as _os
                    mtime = int(_os.path.getmtime(full_path))
                    self.send_header('ETag', f'"{mtime:x}"')
                    self.send_header('Last-Modified', self.date_time_string(mtime))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception as e:
                    self.send_json({'error': str(e)}, 500)
                    return
            else:
                self.send_json({'error': 'File not found'}, 404)
                return
        
        # API endpoints
        if path == '/api/login':
            # Login endpoint
            query = parse_qs(parsed.query)
            password = query.get('password', [''])[0]
            
            if verify_password(password):
                token = generate_token()
                active_tokens[token] = True
                log_activity('Successful login', 'success')
                self.send_json({'success': True, 'token': token})
            else:
                log_activity('Failed login attempt', 'error')
                self.send_json({'success': False, 'error': 'Invalid password'}, 401)
            return
        
        # Public health check (no auth) — used by selenaastra.com/ outward-facing site
        # to show "selena is online" status dot. Must be fast and side-effect-free.
        # NOTE (2026-06-04 per Arcurus): this is the ONLY public API besides
        # /api/login. Every other /api/* endpoint must call self.authenticate().
        if path == '/api/health':
            self.send_json({
                'ok': True,
                'service': 'selena-api',
                'uptime_s': int(time.time() - API_START_TS)
            })
            return

        # Protected endpoints
        if path == '/api/logout':
            # Auth required (added 2026-06-04 per Arcurus). Without auth, an
            # unauth'd caller could attempt to invalidate arbitrary tokens;
            # the in-memory check would silently no-op for unknown tokens,
            # but the principle is still that logout is a sensitive op.
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            auth_header = self.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]
                if token in active_tokens:
                    del active_tokens[token]
            self.send_json({'success': True})
            return

        if path == '/api/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json({
                'authenticated': True,
                'agents': get_agent_info(),
                'file_count': len(get_files_recursive(SELENA_ROOT)),
                'memory_count': len(get_memory_structure())
            })
            return
        
        if path == '/api/files':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json({'files': get_files_recursive(SELENA_ROOT)})
            return
        
        if path == '/api/memory':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json({'memories': get_memory_structure()})
            return
        
        if path == '/api/agents':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json({'agents': get_agent_info()})
            return
        
        # World Scheduler endpoints
        if path == '/api/world/scheduler/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json(scheduler.status())
            return
        
        if path == '/api/world/scheduler/start':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            result = scheduler.start()
            self.send_json(result)
            return
        
        if path == '/api/world/scheduler/stop':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            result = scheduler.stop()
            self.send_json(result)
            return
        
        if path == '/api/world/scheduler/trigger':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Run one action immediately
            scheduler.execute_scheduled_action()
            self.send_json({'success': True, 'message': 'Action triggered', 'status': scheduler.status()})
            return

        if path == '/api/world/scheduler/config':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # GET reads the current config; POST lives in do_POST (this
            # handler only runs for GET requests, so no method check needed).
            # As of 2026-06-08, the scheduler lives in the open-world-selena
            # Rust binary (src/scheduler.rs), not here. We read the config
            # directly from the world's data dir.
            # NOTE: do NOT add `import os` here — `os` is already imported
            # at module top (line ~12), and a function-local `import os`
            # makes Python treat `os` as a local variable for the WHOLE
            # function. If any earlier branch (e.g. the index.html
            # handler at line ~3567) references `os` before this line
            # runs, you get `UnboundLocalError`. Hard lesson from
            # 2026-06-08: that's exactly what crashed `/` and made
            # the dashboard return 502.
            cfg_path = os.path.join(
                os.path.dirname(__file__), '..', '..', 'open-world-selena',
                'world_data', 'ow_scheduler_config.json'
            )
            cfg = {}
            try:
                if os.path.exists(cfg_path):
                    with open(cfg_path, 'r') as f:
                        cfg = json.load(f)
            except Exception as e:
                log_api('OW_SCHEDULER_CONFIG_READ_FAIL', f'path={cfg_path} err={e}')
            self.send_json({
                'success': True,
                'config': cfg,
                'status': {
                    'note': 'scheduler now lives in open-world-selena/src/scheduler.rs (Rust tokio task)',
                    'config_path': cfg_path,
                },
            })
            return

        # Priority Reflector endpoints
        if path == '/api/priority/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json(reflector.status())
            return
        
        if path == '/api/priority/suggest':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            suggestion = reflector.suggest_next_action()
            self.send_json({'success': True, 'suggestion': suggestion})
            return
        
        if path == '/api/priority/add':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Parse query params for task details
            query = parse_qs(parsed.query)
            name = query.get('name', [''])[0]
            description = query.get('description', [''])[0]
            if not name:
                self.send_json({'success': False, 'error': 'Task name required'}, 400)
                return
            # Extract scores from query params
            scores = {}
            for key in ['impact', 'urgency', 'effort', 'dependencies', 'learning', 'joy']:
                if key in query:
                    try:
                        scores[key] = int(query[key][0])
                    except ValueError:
                        pass
            task = reflector.add_task(name, description, **scores)
            self.send_json({'success': True, 'task': task.to_dict()})
            return
        
        if path == '/api/priority/clear':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            reflector.tasks = []
            self.send_json({'success': True, 'message': 'All tasks cleared'})
            return
        
        # Self-Evolution Loop endpoints
        if path == '/api/evolution/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json(evolution_loop.status())
            return
        
        if path == '/api/evolution/start':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            result = evolution_loop.start()
            self.send_json(result)
            return
        
        if path == '/api/evolution/stop':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            result = evolution_loop.stop()
            self.send_json(result)
            return
        
        if path == '/api/evolution/trigger':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Run one evolution cycle immediately
            evolution_loop.evolve()
            self.send_json({'success': True, 'status': evolution_loop.status()})
            return
        
        if path == '/api/evolution/health':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            health = evolution_loop.check_system_health()
            self.send_json({'success': True, 'health': health})
            return
        
        # LLM Call Tracking endpoint
        if path == '/api/llm-calls':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Read from the same file that scheduled_actions uses
            llm_call_file = os.path.join(os.path.dirname(__file__), '..', 'data', 'llm_calls.json')
            llm_call_count = 0
            try:
                if os.path.exists(llm_call_file):
                    with open(llm_call_file, 'r') as f:
                        data = json.load(f)
                        llm_call_count = data.get('count', 0)
            except:
                pass
            self.send_json({
                'used': llm_call_count,
                'limit': llm_call_limit,
                'remaining': llm_call_limit - llm_call_count,
                'usage_percent': round((llm_call_count / llm_call_limit) * 100, 1) if llm_call_limit > 0 else 0,
                'reset_info': 'Token plan refreshes every 5 hours'
            })
            return

        # Rich multi-provider LLM usage (per-provider quota + local 5h window
        # + per-project allocation).  Backed by llm_call_tracker.py which
        # queries provider APIs (MiniMax token plan /v1/token_plan/remains)
        # and falls back to local sliding-window counts for providers whose
        # credentials OpenClaw manages internally (xAI, OpenRouter).
        if path == '/api/llm-usage':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            t = _get_llm_tracker()
            qs = parse_qs(urlparse(self.path).query)
            if qs.get('sync', ['0'])[0] in ('1', 'true', 'yes'):
                t.sync_quotas(force=True)
            payload = t.status()
            # Merge in-memory per-project counts on top of the snapshot's
            # per_project_5h so direct (non-OpenClaw) calls — e.g. the
            # open-world-selena Rust binary's LLM calls under project
            # 'open-world-running' — show up in the 5h per-project view
            # in real time. The snapshot is OpenClaw-session-driven and
            # only refreshes on the 10-sec budget-gate / llm-usage-sync
            # timer (was 5 min before 2026-06-11), so without this overlay
            # the OW sim's spend would lag by up to the refresh window.
            # Added 2026-06-09 per Arcurus #cost-tracker.
            try:
                local = payload.setdefault('local', {})
                merged = dict(local.get('per_project_5h') or {})
                for proj, n in (t.per_project_5h_inmem() or {}).items():
                    merged[proj] = merged.get(proj, 0) + n
                local['per_project_5h'] = merged
                # Per-project rollup to parent (e.g. open-world-dev +
                # open-world-running roll up to open-world-selena).
                rollup = {}
                project_defs = _load_project_defs()
                for proj, n in merged.items():
                    parent = project_defs.get(proj, {}).get('parentProject')
                    if parent:
                        rollup[parent] = rollup.get(parent, 0) + n
                local['per_project_5h_rollup'] = rollup
            except Exception as _e:
                # Non-fatal; the user still gets the snapshot data.
                pass
            self.send_json(payload)
            return

        if path == '/api/llm-usage/sync':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            t = _get_llm_tracker()
            self.send_json(t.sync_quotas(force=True))
            return

        # ---- OpenClaw session-usage tracker ----
        # Proper per-turn input/output/cacheRead/cacheWrite token split
        # parsed from per-session .jsonl transcripts. The CLI
        # `python3 code/openclaw_cost_tracker.py {backfill,sync,status}` writes
        # to data/openclaw_usage.jsonl; we serve slices of that log here.
        if path.startswith('/api/openclaw-usage'):
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            if _openclaw_usage is None:
                self.send_json({
                    'error': 'openclaw_usage module not importable',
                    'import_error': _OPENCLAW_USAGE_IMPORT_ERROR,
                }, 500)
                return
            qs = parse_qs(urlparse(self.path).query)
            sub = path[len('/api/openclaw-usage'):].strip('/')
            if sub in ('', 'stats'):
                # Today / 5h / 24h / all-time summary
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                now = _dt.now(_tz.utc)
                events = list(_openclaw_usage._iter_events())
                def _stats_for(since, until=None):
                    filt = _openclaw_usage._filter_events(events, since=since, until=until)
                    return _openclaw_usage._build_stats(filt)
                out = {
                    'now': now.isoformat(),
                    'log_file': _openclaw_usage.EVENT_LOG,
                    'today': _stats_for(now.replace(hour=0, minute=0, second=0, microsecond=0)),
                    'last_5h': _stats_for(now - _td(hours=5)),
                    'last_24h': _stats_for(now - _td(hours=24)),
                    'all_time': _stats_for(_dt(1970, 1, 1, tzinfo=_tz.utc)),
                }
                self.send_json(out)
                return
            if sub == 'timeseries':
                try:
                    hours = int(qs.get('hours', ['24'])[0])
                except (TypeError, ValueError):
                    hours = 24
                dimension = qs.get('dimension', ['model'])[0]
                if dimension not in ('model', 'provider', 'kind', 'agent', 'project', 'cron'):
                    dimension = 'model'
                hours = max(1, min(hours, 168))
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                now = _dt.now(_tz.utc)
                events = _openclaw_usage._filter_events(
                    _openclaw_usage._iter_events(),
                    since=now - _td(hours=hours),
                )
                out = _openclaw_usage._bucketize(events, hours, dimension=dimension)
                self.send_json(out)
                return
            if sub == 'sessions':
                # Most recent sessions (paged)
                try:
                    limit = int(qs.get('limit', ['50'])[0])
                except (TypeError, ValueError):
                    limit = 50
                try:
                    offset = int(qs.get('offset', ['0'])[0])
                except (TypeError, ValueError):
                    offset = 0
                limit = max(1, min(limit, 500))
                offset = max(0, offset)
                events = list(_openclaw_usage._iter_events())
                # Sort by updatedAt desc; missing goes to end
                def _k(e):
                    ua = e.get('updatedAt')
                    if isinstance(ua, (int, float)):
                        return ua
                    if isinstance(ua, str):
                        try:
                            return _dt.fromisoformat(ua.replace('Z', '+00:00')).timestamp() * 1000
                        except (ValueError, AttributeError):
                            return 0
                    return 0
                events.sort(key=_k, reverse=True)
                page = events[offset:offset + limit]
                self.send_json({
                    'total': len(events),
                    'limit': limit,
                    'offset': offset,
                    'sessions': page,
                })
                return
            if sub == 'cron-jobs':
                # Resolve cron-job IDs to friendly names via openclaw cron list
                try:
                    import subprocess as _sp
                    proc = _sp.run(
                        ['openclaw', 'cron', 'list', '--json'],
                        capture_output=True, text=True, timeout=10,
                    )
                    if proc.returncode == 0:
                        try:
                            jobs = json.loads(proc.stdout)
                        except json.JSONDecodeError:
                            jobs = []
                    else:
                        jobs = []
                except Exception as e:  # noqa: BLE001
                    jobs = []
                id_to_name = {}
                items = jobs.get('jobs') if isinstance(jobs, dict) else jobs
                if isinstance(items, list):
                    for j in items:
                        if isinstance(j, dict):
                            jid = j.get('id') or j.get('jobId')
                            nm = j.get('name') or '?'
                            if jid:
                                id_to_name[jid] = nm
                self.send_json({'jobs': id_to_name})
                return
            if sub == 'channels':
                # Resolve Discord channel IDs to friendly names.
                # Per Arcurus 2026-06-12 #project-selena: "under
                # Recent sessions, can you display also the related
                # discord channel or cron name". The cost-tracker
                # v4 schema (2026-06-12) now records ``channelId``
                # in each session row; this endpoint feeds the
                # web UI the ID → name map it needs to render the
                # channel column. Reads from the explicit allowlist
                # ``data/discord_known_channels.json`` (the same
                # file ``code/discord_client.py::collect_known_channels``
                # reads — single source of truth, no extra Discord
                # API call). Falls back to {} on any read error so
                # the UI keeps working with channelId-only labels.
                id_to_name = {}
                try:
                    allowlist = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        '..', 'data', 'discord_known_channels.json',
                    )
                    allowlist = os.path.normpath(allowlist)
                    with open(allowlist, encoding='utf-8') as _f:
                        cfg = json.loads(_f.read())
                    for c in (cfg.get('channels') or []):
                        if not isinstance(c, dict):
                            continue
                        cid = c.get('id')
                        cname = c.get('name')
                        if cid and cname:
                            id_to_name[str(cid)] = str(cname)
                except (OSError, json.JSONDecodeError, ValueError) as _e:
                    log_error(f'/api/openclaw-usage/channels read failed: {_e}', 'channels')
                self.send_json({'channels': id_to_name})
                return
            if sub == 'sync':
                # Force a sync (reads sessions.json + per-session .jsonl)
                counts = _openclaw_usage.cmd_sync(argparse.Namespace())
                self.send_json({'ok': True, 'counts': counts})
                return
            # Unknown sub
            self.send_json({'error': f'unknown sub: {sub}'}, 404)
            return

        if path == '/api/llm-usage/sync':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            t = _get_llm_tracker()
            self.send_json(t.sync_quotas(force=True))
            return
            t = _get_llm_tracker()
            self.send_json(t.sync_quotas(force=True))
            return

        # Time-series buckets for the web-UI line chart.  Hourly buckets,
        # per-provider counts + total.  Window: 1h..168h (1 week).
        if path == '/api/llm-usage/timeseries':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                hours = int(qs.get('hours', ['24'])[0])
            except (TypeError, ValueError):
                hours = 24
            t = _get_llm_tracker()
            self.send_json(t.get_timeseries(hours))
            return

        if path.startswith('/api/llm-usage/budget'):
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            project = qs.get('project', ['open-world-selena'])[0]
            additional = int(qs.get('additional', ['1'])[0])
            t = _get_llm_tracker()
            self.send_json(t.check_budget(project, additional))
            return

        # Per-project drill-down (todo 8c269253, added 2026-06-06).
        # Returns all-time (or last N days) totals per project, with
        # model/provider breakdown and last-call timestamp.  Backed by
        # the JSONL event log so it survives deque rollover.
        if path == '/api/llm-usage/per-project':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                days = int(qs.get('days', ['7'])[0])
            except (TypeError, ValueError):
                days = 7
            t = _get_llm_tracker()
            self.send_json(t.per_project_breakdown(days=days))
            return

        # Per-model cost breakdown (added 2026-06-10 per Arcurus
        # #lunar-project).  Powers the "Cost by Model" sub-tab in the
        # web UI.  Runs the price audit (llm_price_audit.py) which
        # aggregates events + openclaw usage, then prices each model's
        # tokens using the hardcoded llm_pricing.py table (cache
        # tokens at the model's actual cache_read/cache_write rate).
        if path == '/api/llm-usage/per-model-cost':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            force = qs.get('force', ['0'])[0] in ('1', 'true', 'yes')
            try:
                window_hours = float(qs.get('window_hours', ['24'])[0])
            except (TypeError, ValueError):
                window_hours = 24.0
            try:
                from llm_price_audit import run_audit
                report = run_audit(window_hours=window_hours)
                # The audit appends to data/llm_price_audit.jsonl each
                # time it runs; we don't need to re-append on every
                # poll.  When `force=1` (user clicks "Sync now" in the
                # UI), the run_audit() call above already wrote the
                # new line, so just return it.
                self.send_json(report)
            except Exception as e:  # noqa: BLE001
                self.send_json({
                    'error': f'audit_failed: {e}',
                    'at': datetime.now(timezone.utc).isoformat(),
                    'by_model': [],
                    'missing_prices': [],
                    'drift_flags': [],
                }, 500)
            return

        # Pre-action gate (per Arcurus 2026-06-03: "postpone autonomous
        # or resource intensive tasks until the next refresh").
        if path.startswith('/api/llm-usage/check') \
                or path.startswith('/api/llm-usage/gate'):
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            project = qs.get('project', [None])[0] or None
            additional = int(qs.get('additional', ['1'])[0])
            t = _get_llm_tracker()
            self.send_json(t.should_proceed(project=project,
                                            additional_calls=additional))
            return

        if path.startswith('/api/llm-usage/wait'):
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            project = qs.get('project', [None])[0] or None
            additional = int(qs.get('additional', ['1'])[0])
            max_wait = int(qs.get('max-wait-s', ['1800'])[0])
            t = _get_llm_tracker()
            self.send_json(t.wait_until_budget(project=project,
                                                additional_calls=additional,
                                                max_wait_s=max_wait))
            return

        # Budget alert state (read-only + manual override)
        if path == '/api/llm-usage/alert-state':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            t = _get_llm_tracker()
            self.send_json(t.alert_state())
            return

        if path == '/api/llm-usage/alert-test':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            t = _get_llm_tracker()
            rec = t.evaluate_alerts(force=True)
            self.send_json({'fired': rec is not None, 'record': rec})
            return

        if path == '/api/llm-usage/alert-reset':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            t = _get_llm_tracker()
            t.reset_alerts()
            self.send_json({'success': True})
            return

        if path == '/api/llm-usage/record':
            # Accept either a user session (Bearer / cookie) OR the static
            # service token.  Service token is what the Open World Rust
            # server and scheduled_actions.py use.
            authed = self.authenticate()
            if not authed and LLM_RECORD_TOKEN:
                hdr = self.headers.get('Authorization', '')
                if hdr.startswith('Bearer '):
                    authed = (hdr[7:] == LLM_RECORD_TOKEN)
            if not authed:
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            provider = qs.get('provider', [''])[0]
            model = qs.get('model', [''])[0]
            project = qs.get('project', [''])[0]
            ti = qs.get('tokens_in', [None])[0]
            to = qs.get('tokens_out', [None])[0]
            rt = qs.get('reasoning_tokens', [None])[0]
            ci = qs.get('chars_in', [None])[0]
            co = qs.get('chars_out', [None])[0]
            cr = qs.get('chars_reasoning', [None])[0]
            # Optional session/message ids for dedup against the
            # reconciler's events (added 2026-06-08). Without these
            # the record is in-memory only (no file write, no double-
            # tracking risk).
            sid = qs.get('session_id', [None])[0]
            mid = qs.get('message_id', [None])[0]
            if not provider or not model:
                self.send_json({'error': 'provider and model required'}, 400)
                return
            t = _get_llm_tracker()
            result = t.record(provider, model, project=project,
                              tokens_in=int(ti) if ti else None,
                              tokens_out=int(to) if to else None,
                              reasoning_tokens=int(rt) if rt else None,
                              chars_in=int(ci) if ci else None,
                              chars_out=int(co) if co else None,
                              chars_reasoning=int(cr) if cr else None,
                              session_id=sid,
                              message_id=mid)
            self.send_json({'success': bool(result.get('ok')), **result})
            return

        # Manual pause/resume of a provider's polling (e.g. when you know
        # the credits are out and don't want the tracker to hammer the API).
        if path == '/api/llm-usage/pause':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            provider = qs.get('provider', [''])[0]
            seconds = int(qs.get('seconds', ['1800'])[0])
            reason = qs.get('reason', ['manual'])[0]
            if not provider:
                self.send_json({'error': 'provider required'}, 400)
                return
            t = _get_llm_tracker()
            t.pause_provider(provider, seconds, reason)
            self.send_json({'success': True, 'provider': provider,
                            'paused_for_s': seconds, 'reason': reason})
            return

        if path == '/api/llm-usage/resume':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            provider = qs.get('provider', [''])[0]
            if not provider:
                self.send_json({'error': 'provider required'}, 400)
                return
            t = _get_llm_tracker()
            t.resume_provider(provider)
            self.send_json({'success': True, 'provider': provider})
            return

        if path == '/api/llm-calls/increment':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            count = track_llm_call()
            self.send_json({'success': True, 'count': count})
            return

        # ===== Cost-tracker endpoints (per Arcurus 2026-06-03) =====
        # JSON: { header, sections, data } for the daily or weekly report.
        if path == '/api/cost-tracker':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            weekly = qs.get('weekly', ['0'])[0] in ('1', 'true', 'yes')
            date = qs.get('date', [None])[0]
            report = _ct_build_weekly_report() if weekly else _ct_build_daily(date)
            self.send_json(report)
            return

        # Markdown render — useful for the web UI to display the same text
        # that gets posted to #cost-tracker.
        if path == '/api/cost-tracker/markdown':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            weekly = qs.get('weekly', ['0'])[0] in ('1', 'true', 'yes')
            date = qs.get('date', [None])[0]
            report = _ct_build_weekly_report() if weekly else _ct_build_daily(date)
            text = _ct_render(report)
            self.send_text(text)
            return

        # Manually trigger a post to #cost-tracker (or override channel).
        if path == '/api/cost-tracker/post':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            channel = qs.get('channel', [None])[0]
            date = qs.get('date', [None])[0]
            weekly = qs.get('weekly', ['0'])[0] in ('1', 'true', 'yes')
            result = _ct_post(channel_id=channel, date=date, weekly=weekly)
            self.send_json(result)
            return

        # ===== World backup endpoints (per Arcurus 2026-06-03) =====
        # Daily snapshot of open-world-selena/world_data/save.owbl into
        # .../backups/save-daily-YYYYMMDD.owbl, 30-day rotation, and
        # a Discord warning if the new backup suddenly loses > 50% of
        # the previous size (catches silent wipe / corruption).

        if path == '/api/world/backup/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            from world_backup import list_daily_backups
            backups = list_daily_backups()
            total = sum(b["size_bytes"] for b in backups)
            self.send_json({
                'success': True,
                'count': len(backups),
                'newest': backups[0]["date_iso"] if backups else None,
                'oldest': backups[-1]["date_iso"] if backups else None,
                'total_bytes': total,
                'retention_days': 30,
                'warn_ratio': 0.5,
            })
            return

        if path == '/api/world/backup/list':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            from world_backup import list_daily_backups
            self.send_json({
                'success': True,
                'count': len(list_daily_backups()),
                'backups': list_daily_backups(),
            })
            return

        if path == '/api/world/backup/run':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            from world_backup import take_daily_backup
            qs = parse_qs(urlparse(self.path).query)
            warn_channel = qs.get('channel', [None])[0]
            dry_run = qs.get('dry_run', ['0'])[0] in ('1', 'true', 'yes')
            if dry_run:
                os.environ['WORLD_BACKUP_DRY_RUN'] = '1'
            result = take_daily_backup(warn_channel_id=warn_channel)
            self.send_json({'success': result.get('ok', False), 'result': result})
            return

        # ===== Discord notifier endpoints (per Arcurus 2026-06-03) =====
        # The cron announce mode is broken (Unsupported channel error on
        # the slow-heartbeat / moderation jobs).  All cron jobs SHOULD
        # use these endpoints (or the post-to-discord.sh CLI) to send
        # announcements, instead of relying on OpenClaw's delivery
        # pipeline.  Every send is logged to data/discord_send_log.jsonl
        # with project / agent / task tags for downstream stats.

        if path == '/api/discord/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            n = get_default_notifier()
            if not n.enabled:
                n.start()
            self.send_json(n.status())
            return

        if path == '/api/discord/send':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            channel_id = qs.get('channel', [''])[0] or None
            project = qs.get('project', [''])[0]
            agent = qs.get('agent', [''])[0]
            task = qs.get('task', [''])[0]
            # Per-agent channel default (todo 395bb4b0): if the caller
            # forgot to pass `channel=` and the agent has a known default
            # (e.g. slow-heartbeat -> #heartbeats), use it instead of
            # silently falling back to the notifier's #selena-project
            # default.  This prevents recurring misroutes from cron jobs
            # that build the curl without an explicit channel.
            if not channel_id and agent and agent in AGENT_DEFAULT_CHANNELS:
                channel_id = AGENT_DEFAULT_CHANNELS[agent]
            # text is the raw body (POST) or 'text' query param (GET)
            text = ''
            if self.command == 'POST':
                cl = int(self.headers.get('Content-Length', 0) or 0)
                if cl:
                    text = self.rfile.read(cl).decode('utf-8', errors='replace')
            if not text:
                text = qs.get('text', [''])[0]
            if not text:
                self.send_json({'success': False, 'error': 'empty text'}, 400)
                return
            n = get_default_notifier()
            if not n.enabled:
                n.start()
            if not n.enabled:
                self.send_json({'success': False, 'error': 'notifier disabled (no token)'}, 503)
                return
            ok = n.send_message(channel_id, text,
                                project=project, agent=agent, task=task)
            self.send_json({'success': ok, 'channel_id': channel_id or n.default_channel_id,
                            'project': project, 'agent': agent, 'task': task,
                            'length': len(text)})
            return

        if path == '/api/discord/stats':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            from discord_client import send_log_stats
            self.send_json(send_log_stats())
            return

        if path == '/api/discord/recent':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = int(qs.get('limit', ['20'])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(limit, 1000))
            from discord_client import read_send_log
            entries = read_send_log(limit=limit)
            self.send_json({'count': len(entries), 'entries': entries})
            return

        # ===== Cron job tracker endpoints (per Arcurus 2026-06-03) =====
        # All require auth.  Mutating endpoints log_api on every change.
        # The supported operations mirror the cron_tracker.py CLI.

        if path == '/api/cron/list':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            jobs = _cron_list_jobs()
            self.send_json({'count': len(jobs), 'jobs': jobs})
            return

        if path == '/api/cron/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            ref = qs.get('ref', [''])[0]
            if not ref:
                self.send_json({'error': 'ref required'}, 400)
                return
            s = _cron_get_summary(ref)
            if s is None:
                self.send_json({'error': f"no unique job matching '{ref}'"}, 404)
                return
            self.send_json(s)
            return

        if path == '/api/cron/enable':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            ref = qs.get('ref', [''])[0]
            if not ref:
                self.send_json({'error': 'ref required'}, 400)
                return
            ok = _cron_enable(ref)
            log_api('CRON_ENABLE', f'{ref} (success={ok})')
            self.send_json({'success': ok, 'ref': ref})
            return

        if path == '/api/cron/disable':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            ref = qs.get('ref', [''])[0]
            reason = qs.get('reason', [''])[0]
            if not ref:
                self.send_json({'error': 'ref required'}, 400)
                return
            ok = _cron_disable(ref, reason=reason)
            log_api('CRON_DISABLE', f'{ref} reason={reason!r} (success={ok})')
            self.send_json({'success': ok, 'ref': ref, 'reason': reason})
            return

        if path == '/api/cron/model':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            ref = qs.get('ref', [''])[0]
            model = qs.get('model', [''])[0]
            if not ref or not model:
                self.send_json({'error': 'ref and model required'}, 400)
                return
            ok = _cron_set_model(ref, model)
            log_api('CRON_SET_MODEL', f'{ref} -> {model} (success={ok})')
            self.send_json({'success': ok, 'ref': ref, 'model': model})
            return

        if path == '/api/cron/context':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            ref = qs.get('ref', [''])[0]
            try:
                max_tokens = int(qs.get('max_tokens', [''])[0])
            except ValueError:
                self.send_json({'error': 'max_tokens must be an integer'}, 400)
                return
            if not ref or max_tokens <= 0:
                self.send_json({'error': 'ref and positive max_tokens required'}, 400)
                return
            ok = _cron_set_context(ref, max_tokens)
            log_api('CRON_SET_CONTEXT', f'{ref} -> {max_tokens} (success={ok})')
            self.send_json({'success': ok, 'ref': ref, 'max_tokens': max_tokens})
            return

        if path == '/api/cron/instructions':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            ref = qs.get('ref', [''])[0]
            if not ref:
                self.send_json({'error': 'ref required'}, 400)
                return
            text = _cron_get_instructions(ref)
            if text is None:
                self.send_json({'error': f"no unique job matching '{ref}'"}, 404)
                return
            self.send_json({'ref': ref, 'instructions': text,
                            'length': len(text)})
            return
        
        # Workspace Scanner endpoints
        if path == '/api/worker/scan':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Run the workspace scanner
            result = scan_workspace()
            self.send_json({
                'success': True,
                'timestamp': result.timestamp,
                'files_scanned': result.files_scanned,
                'entries_added': result.entries_added,
                'entries_updated': result.entries_updated,
                'errors': result.errors,
                'details': result.details[:20]  # Limit details to first 20
            })
            return
        
        if path == '/api/worker/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            last = get_last_scan()
            if last:
                self.send_json({
                    'has_result': True,
                    'timestamp': last.timestamp,
                    'files_scanned': last.files_scanned,
                    'entries_added': last.entries_added,
                    'entries_updated': last.entries_updated,
                    'errors': last.errors
                })
            else:
                self.send_json({'has_result': False})
            return
        
        if path == '/api/worker/history':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            history = get_scan_history()
            self.send_json({
                'history': [
                    {
                        'timestamp': h.timestamp,
                        'files_scanned': h.files_scanned,
                        'entries_added': h.entries_added,
                        'entries_updated': h.entries_updated,
                        'errors': h.errors
                    }
                    for h in history[-5:]  # Last 5 scans
                ]
            })
            return
        
        # File Browser endpoints (password protected)
        if path == '/api/files/list':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            dir_path = query.get('path', [''])[0]
            sort_by = query.get('sort_by', ['modified'])[0]  # modified, name, size
            search = query.get('search', [''])[0].lower()
            
            # Security: Only allow accessing workspace directory
            base_path = os.path.expanduser('~/openclaw/workspace')
            if not dir_path:
                dir_path = base_path
            
            # Prevent path traversal
            try:
                full_path = os.path.abspath(os.path.join(base_path, dir_path))
                if not full_path.startswith(base_path):
                    self.send_json({'error': 'Access denied'}, 403)
                    return
            except:
                self.send_json({'error': 'Invalid path'}, 400)
                return
            
            if not os.path.exists(full_path) or not os.path.isdir(full_path):
                self.send_json({'error': 'Directory not found'}, 404)
                return
            
            try:
                entries = []
                for name in sorted(os.listdir(full_path)):
                    entry_path = os.path.join(full_path, name)
                    is_dir = os.path.isdir(entry_path)
                    rel_path = os.path.relpath(entry_path, base_path)
                    
                    # Filter by search term if provided
                    if search and search not in name.lower():
                        continue
                    
                    stat = os.stat(entry_path) if not is_dir else None
                    entries.append({
                        'name': name,
                        'path': rel_path,
                        'is_dir': is_dir,
                        'size': 0 if is_dir else os.path.getsize(entry_path),
                        'modified': datetime.datetime.fromtimestamp(stat.st_mtime).isoformat() if stat else None
                    })
                
                # Sort entries
                reverse = True  # For modified and size, we want largest first (most recent/largest)
                if sort_by == 'name':
                    entries.sort(key=lambda x: x['name'].lower(), reverse=False)
                    reverse = False
                elif sort_by == 'size':
                    entries.sort(key=lambda x: x['size'], reverse=True)
                else:  # modified (default)
                    entries.sort(key=lambda x: x.get('modified') or '', reverse=True)
                
                self.send_json({
                    'success': True, 
                    'path': dir_path, 
                    'entries': entries,
                    'sort_by': sort_by,
                    'search': search if search else None
                })
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return
        
        if path == '/api/files/read':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            file_path = query.get('path', [''])[0]
            
            if not file_path:
                self.send_json({'error': 'File path required'}, 400)
                return
            
            # Security: Only allow accessing workspace directory
            base_path = os.path.expanduser('~/openclaw/workspace')
            
            # Prevent path traversal
            try:
                full_path = os.path.abspath(os.path.join(base_path, file_path))
                if not full_path.startswith(base_path):
                    self.send_json({'error': 'Access denied'}, 403)
                    return
            except:
                self.send_json({'error': 'Invalid path'}, 400)
                return
            
            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                self.send_json({'error': 'File not found'}, 404)
                return
            
            # Limit file size to 1MB
            if os.path.getsize(full_path) > 1024 * 1024:
                self.send_json({'error': 'File too large (max 1MB)'}, 400)
                return
            
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.send_json({'success': True, 'path': file_path, 'content': content})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return
        
        if path == '/api/files/download':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            file_path = query.get('path', [''])[0]
            
            if not file_path:
                self.send_json({'error': 'File path required'}, 400)
                return
            
            # Security: Only allow accessing workspace directory
            base_path = os.path.expanduser('~/openclaw/workspace')
            
            # Prevent path traversal
            try:
                full_path = os.path.abspath(os.path.join(base_path, file_path))
                if not full_path.startswith(base_path):
                    self.send_json({'error': 'Access denied'}, 403)
                    return
            except:
                self.send_json({'error': 'Invalid path'}, 400)
                return
            
            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                self.send_json({'error': 'File not found'}, 404)
                return
            
            # No size limit for download (unlike read which limits to 1MB)
            try:
                # Get file size
                file_size = os.path.getsize(full_path)
                
                # Determine content type
                import mimetypes
                content_type, _ = mimetypes.guess_type(full_path)
                if not content_type:
                    content_type = 'application/octet-stream'
                
                # Get filename from path
                filename = os.path.basename(full_path)
                
                # Send response headers for file download
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                self.send_header('Content-Length', str(file_size))
                self.send_cors_headers()
                self.end_headers()
                
                # Stream file in chunks
                chunk_size = 64 * 1024  # 64KB chunks
                with open(full_path, 'rb') as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                
                log_activity(f'Downloaded file: {file_path}', 'info')
            except Exception as e:
                log_error(f'File download failed: {file_path}', str(e))
                self.send_json({'error': str(e)}, 500)
            return
        
        # Recent files endpoint - returns top N most recently modified files
        if path == '/api/files/recent':
            if not self.authenticate():
                log_api('UNAUTHORIZED', f'Attempted to load recent files without auth')
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                query = parse_qs(parsed.query)
                limit = int(query.get('limit', [10])[0])
                search = query.get('search', [''])[0].lower()
                project = query.get('project', ['selena-project'])[0]
                
                # Build the search directory
                base_path = os.path.expanduser('~/openclaw/workspace')
                search_dir = os.path.join(base_path, project)
                
                if not os.path.exists(search_dir):
                    self.send_json({'error': 'Project not found'}, 404)
                    return
                
                # Get all files recursively
                all_files = get_files_recursive(search_dir)
                
                # Filter by search term if provided (search filename only, like /api/files/list)
                if search:
                    all_files = [f for f in all_files if search in os.path.basename(f['path']).lower()]
                
                # Sort by modified time (most recent first)
                all_files.sort(key=lambda x: x.get('modified') or '', reverse=True)
                
                # Return top N
                recent_files = all_files[:limit]
                
                log_api('RECENT_FILES', f'Loaded {len(recent_files)} recent files for {project}')
                self.send_json({
                    'success': True,
                    'files': recent_files,
                    'count': len(recent_files),
                    'search': search if search else None
                })
            except Exception as e:
                log_error(f'Failed to load recent files', str(e))
                self.send_json({'error': str(e)}, 500)
            return
        
        # Biggest files endpoint - returns top N largest files
        if path == '/api/files/biggest':
            if not self.authenticate():
                log_api('UNAUTHORIZED', f'Attempted to load biggest files without auth')
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                query = parse_qs(parsed.query)
                limit = int(query.get('limit', [10])[0])
                search = query.get('search', [''])[0].lower()
                project = query.get('project', ['selena-project'])[0]
                
                # Build the search directory
                base_path = os.path.expanduser('~/openclaw/workspace')
                search_dir = os.path.join(base_path, project)
                
                if not os.path.exists(search_dir):
                    self.send_json({'error': 'Project not found'}, 404)
                    return
                
                # Get all files recursively
                all_files = get_files_recursive(search_dir)
                
                # Filter by search term if provided (search filename only, like /api/files/list)
                if search:
                    all_files = [f for f in all_files if search in os.path.basename(f['path']).lower()]
                
                # Sort by size (largest first)
                all_files.sort(key=lambda x: x.get('size', 0), reverse=True)
                
                # Return top N
                biggest_files = all_files[:limit]
                
                log_api('BIGGEST_FILES', f'Loaded {len(biggest_files)} biggest files for {project}')
                self.send_json({
                    'success': True,
                    'files': biggest_files,
                    'count': len(biggest_files),
                    'search': search if search else None
                })
            except Exception as e:
                log_error(f'Failed to load biggest files', str(e))
                self.send_json({'error': str(e)}, 500)
            return
        
        # Todo Manager endpoints
        if path == '/api/todos':
            if not self.authenticate():
                log_api('UNAUTHORIZED', f'Attempted to load todos without auth')
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Get all todos
            try:
                query = parse_qs(parsed.query)
                status = query.get('status', [None])[0]
                sort_by = query.get('sort_by', ['priority'])[0]
                sensitive = None
                if 'sensitive' in query:
                    sensitive = query['sensitive'][0].lower() == 'true'
                include_deleted = 'include_deleted' in query and query['include_deleted'][0].lower() == 'true'
                search = query.get('search', [None])[0]
                agent_owner = query.get('agent_owner', [None])[0] or None
                project = query.get('project', [None])[0] or None
                todos = todo_manager.get_all_todos(
                    status=status, sort_by=sort_by, sensitive=sensitive,
                    include_deleted=include_deleted, search=search,
                    agent_owner=agent_owner, project=project
                )
                summary = todo_manager.get_summary(sensitive=sensitive)
                log_api('LOAD_TODOS', f'status={status}, sort={sort_by}, sensitive={sensitive}, agent={agent_owner}, project={project}, count={len(todos)}')
                self.send_json({'todos': todos, 'summary': summary})
            except Exception as e:
                log_error(f'/api/todos failed: {str(e)}', 'GET /api/todos')
                self.send_json({'error': f'Internal error: {str(e)}'}, 500)
            return

        if path == '/api/todos/filter-options':
            # Returns distinct agent_owners + projects (with counts) so the
            # web UI can populate filter dropdowns dynamically.
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                query = parse_qs(parsed.query)
                sensitive = None
                if 'sensitive' in query:
                    sensitive = query['sensitive'][0].lower() == 'true'
                include_deleted = 'include_deleted' in query and query['include_deleted'][0].lower() == 'true'
                opts = todo_manager.get_filter_options(sensitive=sensitive, include_deleted=include_deleted)
                self.send_json(opts)
            except Exception as e:
                log_error(f'/api/todos/filter-options failed: {str(e)}', 'GET /api/todos/filter-options')
                self.send_json({'error': f'Internal error: {str(e)}'}, 500)
            return
        
        if path == '/api/todos/summary':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                sensitive = None
                if 'sensitive' in parsed.query:
                    sensitive = parsed.query.split('sensitive=')[1].split('&')[0].lower() == 'true'
                self.send_json(todo_manager.get_summary(sensitive=sensitive))
            except Exception as e:
                log_error(f'/api/todos/summary failed: {str(e)}', 'GET /api/todos/summary')
                self.send_json({'error': f'Internal error: {str(e)}'}, 500)
            return
        
        if path == '/api/todos/add':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Parse query params (GET) or JSON body (POST)
            query = parse_qs(parsed.query)
            short_desc = query.get('short_desc', [''])[0]
            long_desc = query.get('long_desc', [''])[0]
            priority = int(query.get('priority', ['5'])[0])
            sensitive = query.get('sensitive', ['false'])[0].lower() == 'true'
            parent_id = query.get('parent_id', [None])[0] if 'parent_id=' in parsed.query else None
            estimated_llm_calls = int(query.get('estimated_llm_calls', ['0'])[0]) if 'estimated_llm_calls=' in parsed.query else None
            creator_id = query.get('creator_id', [None])[0] if 'creator_id=' in parsed.query else None
            conversation_id = query.get('conversation_id', [None])[0] if 'conversation_id=' in parsed.query else None
            agent_id = query.get('agent_id', [None])[0] if 'agent_id=' in parsed.query else None
            project = query.get('project', [None])[0] if 'project=' in parsed.query else None
            agent_owner = query.get('agent_owner', [None])[0] if 'agent_owner=' in parsed.query else None
            what_happened = query.get('what_happened', [None])[0] if 'what_happened=' in parsed.query else None
            # FIX (lunar-worker run 75, 2026-06-09 22:50 UTC): irreversible and
            # block_reason are referenced unconditionally on the add_todo call
            # below. Previously they were only set inside the POST body
            # branch, so GET requests hit UnboundLocalError on line 1945,
            # the server's exception handler bailed without sending a
            # response, and clients (urllib, curl GET) saw "Empty reply from
            # server" / "Remote end closed connection without response" —
            # the v2 loose-end extractor connection-reset bug seen in 6+
            # consecutive runs. Initialise to None (the prefilter sentinel)
            # here, and also accept them via query string for GET callers
            # who want the prefilter disabled (irreversible=false) or
            # documented as already-assessed.
            irreversible = (False if 'irreversible=false' in parsed.query else None)
            block_reason = query.get('block_reason', [None])[0] if 'block_reason=' in parsed.query else None
            # For POST requests with JSON body, prefer body fields over query params
            if self.command == 'POST':
                cl = int(self.headers.get('Content-Length', 0) or 0)
                if cl:
                    try:
                        body = json.loads(self.rfile.read(cl).decode('utf-8', errors='replace'))
                        if body.get('short_desc') is not None: short_desc = body['short_desc']
                        if body.get('long_desc') is not None: long_desc = body['long_desc']
                        if body.get('creator_id') is not None: creator_id = body['creator_id']
                        if body.get('conversation_id') is not None: conversation_id = body['conversation_id']
                        if body.get('agent_id') is not None: agent_id = body['agent_id']
                        if body.get('project') is not None: project = body['project']
                        if body.get('agent_owner') is not None: agent_owner = body['agent_owner']
                        if body.get('what_happened') is not None: what_happened = body['what_happened']
                        if body.get('parent_id') is not None: parent_id = body['parent_id']
                        if 'priority' in body:
                            try: priority = int(body['priority'])
                            except (TypeError, ValueError): pass
                        if 'sensitive' in body:
                            sensitive = bool(body['sensitive'])
                        # Body field wins over query string.  Use the
                        # sentinel (None) when absent so the prefilter
                        # can still fire; the body has to explicitly
                        # contain 'irreversible' (even as false) to
                        # bypass the prefilter.
                        if 'irreversible' in body:
                            irreversible = bool(body['irreversible'])
                        # block_reason: explicit body value (including
                        # null) wins over query string. Already set from
                        # query string above; only overwrite if body has it.
                        if 'block_reason' in body:
                            block_reason = body['block_reason']
                    except Exception as e:
                        log_error(f'POST /api/todos/add bad json: {e}', 'add')
            if not short_desc:
                self.send_json({'success': False, 'error': 'short_desc required'}, 400)
                return
            # Parse force flag (bypass semantic dedup)
            force = False
            if 'force' in query:
                force = query.get('force', ['0'])[0].lower() in ('1', 'true', 'yes')
            if self.command == 'POST' and cl:
                try:
                    # body already parsed above for other fields
                    if 'force' in body:
                        force = bool(body['force'])
                except Exception:
                    pass
            # Defensive dedup (added 2026-06-03): if short_desc + creator_id
            # match an existing open todo, skip creating a new one and return
            # the existing record.  Protects against client-side double-submits
            # and the todo_manager double-append bug regressing.
            if creator_id and short_desc:
                existing = todo_manager.find_open_by_signature(short_desc, creator_id)
                if existing:
                    log_api('TODO_DUP_SKIP', f'skipped duplicate add for {creator_id}: {short_desc[:60]}')
                    self.send_json({'success': True, 'todo': existing, 'deduped': True})
                    return
            try:
                result = todo_manager.add_todo(short_desc, long_desc, priority, sensitive, parent_id, estimated_llm_calls, creator_id, conversation_id, agent_id, project, agent_owner, what_happened, irreversible, block_reason, force=force)
                self.send_json({'success': True, **result})
            except TodoDuplicateError as dup:
                # v0.8 dedup bands: 409 carries the band so the web UI can
                # decide between "edit / use / force" (conflict, force_offered)
                # and a hard "already exists" (duplicate, no force escape).
                # force=true is honoured only when force_offered=True; for
                # the duplicate band we still return 409 but the UI must
                # hide the "Add anyway" button.
                log_api(
                    'TODO_SEMANTIC_CONFLICT',
                    f'band={"duplicate" if not dup.force_offered else "conflict"} '
                    f'for {short_desc[:60]} with {dup.existing.get("id")} '
                    f'score={dup.existing.get("_score")}'
                )
                self.send_json({
                    'success': False,
                    'error': 'semantic duplicate',
                    'band': 'duplicate' if not dup.force_offered else 'conflict',
                    'force_offered': dup.force_offered,
                    'existing': dup.existing,
                    'similar': dup.similar,
                }, 409)
            return

        # =====================================================================
        # /api/dedup/similar/<id> — drill-down helper for the "show similar
        # tasks" web UI button.  Given an existing todo id, return the top_k
        # most similar active todos (v0.8 band ladder).  The web modal uses
        # this to populate the similar list AND to power recursive drill-down
        # (click a similar → re-call this endpoint with that id).
        # =====================================================================
        if path.startswith('/api/dedup/similar/'):
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            todo_id = path[len('/api/dedup/similar/'):].strip()
            if not todo_id:
                self.send_json({'success': False, 'error': 'todo id required'}, 400)
                return
            # `query` may not be set in this code path (it is only
            # initialised inside the earlier /api/todos/* branches);
            # parse it here so top_k / min_score work consistently.
            local_query = parse_qs(parsed.query)
            try:
                top_k = int(local_query.get('top_k', ['10'])[0])
            except (TypeError, ValueError):
                top_k = 10
            try:
                min_score = float(local_query.get('min_score', ['0.5'])[0])
            except (TypeError, ValueError):
                min_score = 0.5
            try:
                import todo_embeddings
                results = todo_embeddings.find_similar_to_todo(
                    todo_id, top_k=top_k, min_score=min_score
                )
                target = todo_manager.get_todo(todo_id) or {}
                self.send_json({
                    'success': True,
                    'target': {
                        'id': target.get('id'),
                        'short_desc': target.get('short_desc'),
                        'status': target.get('status'),
                        'project': target.get('project'),
                        'priority': target.get('priority'),
                    } if target else {'id': todo_id, 'short_desc': '', 'status': 'missing'},
                    'similar': results,
                    'top_k': top_k,
                    'min_score': min_score,
                })
            except Exception as exc:
                log_error(f'/api/dedup/similar/{todo_id} failed: {exc}', 'dedup-similar')
                self.send_json({'success': False, 'error': str(exc)}, 500)
            return

        # =====================================================================
        # /api/dedup/similar-counts?min_score=0.5 — bulk count of similar
        # tasks for every active todo.  Powers the "🔍 similar (N)" badge
        # on each todo card so the user can see at a glance which todos
        # have semantic relatives.  One bulk request per page load, cached
        # client-side; the server uses numpy vectorised cosine to compute
        # the full similarity matrix in a single matrix multiply.
        # =====================================================================
        if path == '/api/dedup/similar-counts':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            local_query = parse_qs(parsed.query)
            try:
                min_score = float(local_query.get('min_score', ['0.5'])[0])
            except (TypeError, ValueError):
                min_score = 0.5
            try:
                top_k_per_todo = int(local_query.get('top_k', ['0'])[0])
            except (TypeError, ValueError):
                top_k_per_todo = 0
            try:
                import todo_embeddings
                result = todo_embeddings.compute_similar_counts(
                    min_score=min_score,
                    top_k_per_todo=top_k_per_todo,
                )
                self.send_json({
                    'success': True,
                    'counts': result.get('counts', {}),
                    'min_score': min_score,
                    'top_k_per_todo': top_k_per_todo,
                    'n_todos': result.get('n_todos', 0),
                    'n_pairs': result.get('n_pairs', 0),
                    'compute_ms': result.get('compute_ms', 0),
                })
            except Exception as exc:
                log_error(f'/api/dedup/similar-counts failed: {exc}', 'dedup-counts')
                self.send_json({'success': False, 'error': str(exc)}, 500)
            return

        # =====================================================================
        # /api/dedup/stats — live counter for the cost-tracker-style panel.
        # Returns the by_action counts, score distribution, top-5 most
        # blocked todos, and a force_rate so we can see how often the
        # --force escape hatch is being used.  Optional ?since=<iso>
        # filter for the "last 24h" view.
        # =====================================================================
        if path == '/api/dedup/stats':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # See similar branch above: `query` may not be defined in
            # this dispatch path.  Re-parse locally.
            local_query = parse_qs(parsed.query)
            since = local_query.get('since', [None])[0] if 'since=' in parsed.query else None
            try:
                import todo_embeddings
                stats = todo_embeddings.summarize_dedup_stats(since_iso=since)
                # Also expose the current band thresholds so the UI can
                # show them in the legend (no magic numbers in JS).
                self.send_json({
                    'success': True,
                    'stats': stats,
                    'thresholds': {
                        'duplicate_title': todo_embeddings.THRESHOLD_DUPLICATE_TITLE,
                        'duplicate_cosine': todo_embeddings.THRESHOLD_DUPLICATE_COSINE,
                        'conflict': todo_embeddings.THRESHOLD_HIGH,
                        'hint': todo_embeddings.THRESHOLD_LOW,
                        'weak_floor': 0.5,
                    },
                    'model': todo_embeddings.DEFAULT_MODEL,
                    'since': since,
                })
            except Exception as exc:
                log_error(f'/api/dedup/stats failed: {exc}', 'dedup-stats')
                self.send_json({'success': False, 'error': str(exc)}, 500)
            return

        if path == '/api/todos/update':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            todo_id = query.get('id', [''])[0]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            # Extract update fields
            updates = {}
            if 'short_desc' in query: updates['short_desc'] = query['short_desc'][0]
            if 'long_desc' in query: updates['long_desc'] = query['long_desc'][0]
            if 'priority' in query: updates['priority'] = int(query['priority'][0])
            if 'status' in query: updates['status'] = query['status'][0]
            if 'deleted_at' in query: updates['deleted_at'] = query['deleted_at'][0] if query['deleted_at'][0] else None
            if 'sensitive' in query: updates['sensitive'] = query['sensitive'][0].lower() == 'true'
            if 'parent_id' in query: updates['parent_id'] = query['parent_id'][0] if query['parent_id'][0] else None
            if 'estimated_llm_calls' in query: updates['estimated_llm_calls'] = int(query['estimated_llm_calls'][0]) if query['estimated_llm_calls'][0] else None
            if 'creator_id' in query: updates['creator_id'] = query['creator_id'][0] if query['creator_id'][0] else None
            if 'conversation_id' in query: updates['conversation_id'] = query['conversation_id'][0] if query['conversation_id'][0] else None
            if 'agent_id' in query: updates['agent_id'] = query['agent_id'][0] if query['agent_id'][0] else None
            if 'project' in query: updates['project'] = query['project'][0] if query['project'][0] else None
            if 'agent_owner' in query: updates['agent_owner'] = query['agent_owner'][0] if query['agent_owner'][0] else None
            if 'what_happened' in query: updates['what_happened'] = query['what_happened'][0] if query['what_happened'][0] else None
            if 'irreversible' in query: updates['irreversible'] = query['irreversible'][0].lower() == 'true'
            if 'block_reason' in query: updates['block_reason'] = query['block_reason'][0] if query['block_reason'][0] else None
            if 'waiting_for' in query: updates['waiting_for'] = query['waiting_for'][0] if query['waiting_for'][0] else None
            # completed_at: explicit value wins over the auto-rule. Empty string
            # is treated as "not set" so the auto-rule (set/clear on status
            # transitions) handles it. See TodoManager._apply_completed_at_rule.
            if 'completed_at=' in parsed.query:
                _ca = query.get('completed_at', [''])[0]
                updates['completed_at'] = _ca if _ca else None
            if 'restore' in query: updates['restore'] = query['restore'][0].lower() == 'true'
            todo = todo_manager.update_todo(todo_id, **updates)
            if todo:
                self.send_json({'success': True, 'todo': todo})
            else:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
            return
        
        if path == '/api/todos/children':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            parent_id = query.get('parent_id', [''])[0]
            if not parent_id:
                self.send_json({'success': False, 'error': 'parent_id required'}, 400)
                return
            children = todo_manager.get_children(parent_id)
            self.send_json({'children': children})
            return
        
        if path == '/api/todos/split':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            todo_id = query.get('id', [''])[0]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            # Parse subtasks from query
            subtask_titles = query.get('subtasks', [''])[0].split('|||') if 'subtasks=' in parsed.query else []
            if not subtask_titles or not subtask_titles[0]:
                self.send_json({'success': False, 'error': 'subtasks required (comma or ||| separated)'}, 400)
                return
            subtasks = []
            for title in subtask_titles:
                if title.strip():
                    subtasks.append({'short_desc': title.strip()})
            created = todo_manager.split_todo(todo_id, subtasks)
            if created is None:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
            else:
                self.send_json({'success': True, 'subtasks': created})
            return
        
        if path == '/api/todos/delete':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            todo_id = query.get('id', [''])[0]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            deleted = todo_manager.delete_todo(todo_id)
            self.send_json({'success': deleted})
            return
        
        if path == '/api/todos/mark-done':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            todo_id = query.get('id', [''])[0]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            what_happened = query.get('what_happened', [None])[0] if 'what_happened=' in parsed.query else None
            todo = todo_manager.mark_done(todo_id, what_happened=what_happened)
            if todo:
                self.send_json({'success': True, 'todo': todo})
            else:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
            return

        # /api/todos/reload — force the in-memory todo list to re-read from
        # data/todos.json. Useful after manual file edits (vim, scripts,
        # backup restore).  Added 2026-06-05 per selena-project-worker to
        # address loose-end todo 31e876a4 ("API server in-memory state can
        # desync from data/todos.json"). Supports GET and POST.
        if path == '/api/todos/reload':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            was_stale = todo_manager.is_stale()
            result = todo_manager.reload()
            log_api('TODO_RELOAD', f'regular={result["regular"]} sensitive={result["sensitive"]} stale={result["stale"]}')
            self.send_json({'success': True, 'reloaded': result, 'was_stale': was_stale})
            return

        if path == '/api/todos/mark-blocked':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            todo_id = query.get('id', [''])[0]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            block_reason = query.get('block_reason', [''])[0] if 'block_reason=' in parsed.query else ''
            waiting_for = query.get('waiting_for', [''])[0] if 'waiting_for=' in parsed.query else None
            todo = todo_manager.mark_blocked(todo_id, block_reason=block_reason, waiting_for=waiting_for)
            if todo:
                self.send_json({'success': True, 'todo': todo})
            else:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
            return
        
        if path == '/api/todos/unblock':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            todo_id = query.get('id', [''])[0]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            todo = todo_manager.unblock(todo_id)
            if todo:
                self.send_json({'success': True, 'todo': todo})
            else:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
            return
        
        if path == '/api/todos/backups':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            backups = todo_manager.list_backups()
            self.send_json({'backups': backups})
            return
        
        if path == '/api/todos/restore':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            success = todo_manager.restore_latest()
            if success:
                self.send_json({'success': True, 'message': 'Restored from latest backup'})
            else:
                self.send_json({'success': False, 'error': 'No backups available'}, 404)
            return

        # ===== Failure reporter endpoints (todo 5d1ae721) =====
        # All require auth. Read-only endpoints (list/summary) accept
        # query-string filters that mirror failure_reporter.py CLI flags.
        # Mutating endpoints (acknowledge/resolve/report) call the CLI
        # under selena-project/code/failure_reporter.py — never import
        # the module directly so the CLI surface stays the single source
        # of truth (same pattern as discord-lookup endpoints above).
        # Storage: data/failures.jsonl.

        _failure_cli = [str(Path(SELENA_ROOT) / 'code' / 'failure_reporter.py')]

        if path == '/api/failures' and self.command == 'GET':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                cmd = _failure_cli + ['list', '--format', 'json']
                qs = parse_qs(parsed.query)
                if 'limit' in qs:
                    try:
                        cmd += ['--limit', str(max(0, min(int(qs['limit'][0]), 1000)))]
                    except ValueError:
                        pass
                if qs.get('status', [''])[0]:
                    cmd += ['--status', qs['status'][0]]
                if qs.get('severity', [''])[0]:
                    cmd += ['--severity', qs['severity'][0]]
                if qs.get('agent', [''])[0]:
                    cmd += ['--agent', qs['agent'][0]]
                if qs.get('since', [''])[0]:
                    cmd += ['--since', qs['since'][0]]
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                payload = json.loads(out.stdout)
                self.send_json({
                    'count': payload.get('count', 0),
                    'rows': payload.get('rows', []),
                })
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path == '/api/failures/summary' and self.command == 'GET':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                cmd = _failure_cli + ['summary']
                qs = parse_qs(parsed.query)
                if qs.get('status', [''])[0]:
                    cmd += ['--status', qs['status'][0]]
                if qs.get('since', [''])[0]:
                    cmd += ['--since', qs['since'][0]]
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path.startswith('/api/failures/') and path.endswith('/acknowledge') and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            failure_id = path[len('/api/failures/'):-len('/acknowledge')]
            if not failure_id:
                self.send_json({'error': 'failure_id required'}, 400)
                return
            qs = parse_qs(parsed.query)
            by = qs.get('by', [''])[0] or 'selena-project-worker'
            try:
                out = subprocess.run(
                    _failure_cli + ['acknowledge', failure_id, '--by', by],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode == 1:
                    self.send_json({'error': f'failure {failure_id!r} not found'}, 404)
                    return
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                log_api('FAILURE_ACK', f'id={failure_id} by={by}')
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path.startswith('/api/failures/') and path.endswith('/resolve') and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            failure_id = path[len('/api/failures/'):-len('/resolve')]
            if not failure_id:
                self.send_json({'error': 'failure_id required'}, 400)
                return
            qs = parse_qs(parsed.query)
            note = qs.get('note', [''])[0]
            by = qs.get('by', [''])[0]
            try:
                cmd = _failure_cli + ['resolve', failure_id]
                if note:
                    cmd += ['--note', note]
                if by:
                    cmd += ['--by', by]
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode == 1:
                    self.send_json({'error': f'failure {failure_id!r} not found'}, 404)
                    return
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                log_api('FAILURE_RESOLVE', f'id={failure_id} by={by or "?"}')
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path == '/api/failures/report' and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(parsed.query)
            agent = qs.get('agent', [''])[0]
            severity = qs.get('severity', [''])[0]
            context = qs.get('context', [''])[0]
            message = qs.get('message', [''])[0]
            if not (agent and severity and context and message):
                self.send_json({
                    'error': 'agent, severity, context, message are all required',
                    'received': {
                        'agent': bool(agent), 'severity': bool(severity),
                        'context': bool(context), 'message': bool(message),
                    },
                }, 400)
                return
            try:
                cmd = _failure_cli + [
                    'report', '--agent', agent, '--severity', severity,
                    '--context', context, '--message', message,
                ]
                if qs.get('project', [''])[0]:
                    cmd += ['--project', qs['project'][0]]
                if qs.get('details', [''])[0]:
                    cmd += ['--details', qs['details'][0]]
                if qs.get('notify_channel', [''])[0]:
                    cmd += ['--notify-channel', qs['notify_channel'][0]]
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                log_api('FAILURE_REPORT', f'agent={agent} severity={severity} context={context}')
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # ===== Discord Lookup (new-user scanner + moderation cron trigger) =====
        # Phase 2 (per Arcurus 2026-06-04). All endpoints auth-required.
        # Mutating endpoints (scan, trigger, settings) call the CLI under
        # selena-project/scripts/discord_lookup.py — never import the
        # module directly so the CLI surface stays the single source of truth.

        if path == '/api/discord-lookup/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'), 'status'],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path == '/api/discord-lookup/settings' and self.command == 'GET':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'), 'settings', 'get'],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}'}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path == '/api/discord-lookup/settings' and self.command == 'PATCH':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                body = json.loads(raw) if raw else {}
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json({'error': f'invalid JSON body: {e}'}, 400)
                return
            if not isinstance(body, dict) or not body:
                self.send_json({'error': 'body must be a non-empty JSON object of {key: value}'}, 400)
                return
            # Apply each key/val via the CLI (single source of truth)
            # Special: channels_add and channels_remove_index operate on the channels list
            results = []
            for k, v in body.items():
                if k == 'channels_add':
                    # Read current channels, append, write back
                    get_out = subprocess.run(
                        ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                         'settings', 'get'],
                        capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                    )
                    if get_out.returncode != 0:
                        results.append({'key': k, 'ok': False, 'stderr': 'failed to read current channels'})
                        continue
                    cur = json.loads(get_out.stdout).get('channels', [])
                    if v in cur:
                        results.append({'key': k, 'ok': True, 'stdout': 'already present (no-op)'})
                        continue
                    cur.append(v)
                    out = subprocess.run(
                        ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                         'settings', 'set', '--key', 'channels', '--value', json.dumps(cur)],
                        capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                    )
                    results.append({'key': k, 'ok': out.returncode == 0,
                                    'stdout': out.stdout.strip()[:200],
                                    'stderr': out.stderr.strip()[:200] if out.returncode != 0 else None})
                    continue
                if k == 'channels_remove_index':
                    get_out = subprocess.run(
                        ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                         'settings', 'get'],
                        capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                    )
                    if get_out.returncode != 0:
                        results.append({'key': k, 'ok': False, 'stderr': 'failed to read current channels'})
                        continue
                    cur = json.loads(get_out.stdout).get('channels', [])
                    try:
                        idx = int(v)
                    except (ValueError, TypeError):
                        results.append({'key': k, 'ok': False, 'stderr': 'value must be int index'})
                        continue
                    if idx < 0 or idx >= len(cur):
                        results.append({'key': k, 'ok': False, 'stderr': f'index {idx} out of range (len={len(cur)})'})
                        continue
                    removed = cur.pop(idx)
                    out = subprocess.run(
                        ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                         'settings', 'set', '--key', 'channels', '--value', json.dumps(cur)],
                        capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                    )
                    results.append({'key': k, 'ok': out.returncode == 0,
                                    'stdout': f'removed {removed}',
                                    'stderr': out.stderr.strip()[:200] if out.returncode != 0 else None})
                    continue
                if isinstance(v, (list, dict)):
                    val_str = json.dumps(v)
                elif isinstance(v, bool):
                    val_str = 'true' if v else 'false'
                else:
                    val_str = str(v)
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'settings', 'set', '--key', k, '--value', val_str],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                results.append({'key': k, 'ok': out.returncode == 0, 'stdout': out.stdout.strip()[:200],
                                'stderr': out.stderr.strip()[:200] if out.returncode != 0 else None})
            log_api('DISCORD_LOOKUP_SETTINGS', f'updated {len(body)} key(s)')
            self.send_json({'success': all(r['ok'] for r in results), 'results': results})
            return

        if path == '/api/discord-lookup/triggers':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = int(qs.get('limit', ['20'])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(limit, 500))
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'triggers', '--limit', str(limit)],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}'}, 500)
                    return
                self.send_json({'count': len(json.loads(out.stdout)), 'entries': json.loads(out.stdout)})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path == '/api/discord-lookup/users':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = int(qs.get('limit', ['50'])[0])
            except ValueError:
                limit = 50
            sort = qs.get('sort', ['last_seen'])[0]
            if sort not in ('messages', 'last_seen', 'first_seen'):
                sort = 'last_seen'
            limit = max(1, min(limit, 500))
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'users', '--limit', str(limit), '--sort', sort],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}'}, 500)
                    return
                self.send_json({'count': len(json.loads(out.stdout)), 'users': json.loads(out.stdout)})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path == '/api/discord-lookup/scan' and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Manual scan — uses the CLI's --manual flag for the audit log
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'scan', '--manual'],
                    capture_output=True, text=True, timeout=120, cwd=str(SELENA_ROOT)
                )
                log_api('DISCORD_LOOKUP_SCAN_MANUAL', f'exit={out.returncode}')
                if out.returncode != 0 and not out.stdout:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                # The CLI exits 1 on error decision, 0 otherwise. Both are valid.
                self.send_json(json.loads(out.stdout) if out.stdout else {'error': 'no stdout'})
            except subprocess.TimeoutExpired:
                self.send_json({'error': 'scan timed out after 120s'}, 504)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        if path == '/api/discord-lookup/trigger-cron' and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Force-trigger the moderation cron (bypasses debounce).
            # Useful for forcing a re-evaluation when new user activity
            # is reported by humans (Lenny/Arcurus) outside the scanner.
            try:
                job_id = '1b0f1a2b-5677-4e8e-9699-17c29e55014c'
                out = subprocess.run(
                    ['openclaw', 'cron', 'run', job_id],
                    capture_output=True, text=True, timeout=30
                )
                log_api('DISCORD_LOOKUP_FORCE_TRIGGER', f'exit={out.returncode}')
                if out.returncode != 0:
                    self.send_json({'error': f'openclaw cron run exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                # Update last_wake_at in state
                state_path = Path(SELENA_ROOT) / 'data' / 'moderation_state' / 'discord_lookup_state.json'
                if state_path.exists():
                    st = json.loads(state_path.read_text())
                    st['last_wake_at'] = datetime.datetime.now(timezone.utc).isoformat()
                    st['wake_count'] = st.get('wake_count', 0) + 1
                    state_path.write_text(json.dumps(st, indent=2, ensure_ascii=False))
                self.send_json({'success': True, 'stdout': out.stdout.strip()})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # MiniMax % used (for Last Run sub-tab widget, with 10-sec server-side cache)
        #
        # Per Arcurus 2026-06-10 in #cost-tracker: the widget was reading
        # `providers.minimax.quota.models.*.window_5h.remaining_percent` from
        # the snapshot, but that field was last refreshed 2026-06-08T13:49Z
        # (when the direct `/v1/token_plan/remains` API call last worked)
        # and the value was stuck at 0% remaining (100% used). The snapshot
        # *also* has a fresh `minimax_interval` field that's kept up-to-date
        # by the budget-gate timer (which uses `mmx quota`), but the widget
        # was ignoring it. Now we read from `minimax_interval` first, and
        # fall back to the stale field if it's missing.
        #
        # Authoritative source precedence (highest first):
        #   1. `mmx quota` (called live below) — always up-to-date
        #   2. `snapshot.minimax_interval`     — refreshed every 10 sec by
        #                                        budget-gate.timer (was 5 min
        #                                        before 2026-06-11)
        #   3. `snapshot.providers.minimax.quota` — historical, last seen
        #                                           working 2026-06-08
        if path == '/api/discord-lookup/llm-minimax':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                cached = _minimax_cache_get()
                if cached is not None:
                    self.send_json(cached)
                    return
                # Run `mmx quota` live so the widget never serves stale
                # data even if the snapshot's last refresh is older than
                # the 10-sec server-side cache. Soft-fails: if `mmx`
                # errors, we still fall through to the snapshot fields.
                live_quota = _live_mmx_quota()
                live_models = {}
                live_fetched_at = None
                if live_quota.get("ok"):
                    live_fetched_at = live_quota.get("fetched_at")
                    for entry in (live_quota.get("data") or {}).get("model_remains") or []:
                        name = entry.get("model_name")
                        if not name:
                            continue
                        live_models[name] = {
                            "remaining_percent": entry.get("current_interval_remaining_percent"),
                            "weekly_remaining_percent": entry.get("current_weekly_remaining_percent"),
                            "status": entry.get("current_interval_status"),
                            "resets_in_s": (entry.get("remains_time") or 0) // 1000 if entry.get("remains_time") is not None else None,
                        }
                # Snapshot fallback (used when `mmx` failed or returned no models).
                t = _get_llm_tracker()
                try:
                    t.sync_quotas(force=False)
                except Exception:
                    pass
                status = t.status() or {}
                mi = status.get('minimax_interval') or {}
                snap_models = (mi.get('models') or {}) if mi.get('ok') else {}
                snap_fetched_at = mi.get('fetched_at')
                # Build the per-model window list. Each model gets two
                # windows exposed to the client: `window_5h` and `weekly`
                # (matching the old contract, so the JS code in
                # web/index.html doesn't need changes).
                windows = []
                # Union of model names from live + snapshot so we never
                # silently drop a model just because one source missed it.
                all_models = set(live_models.keys()) | set(snap_models.keys())
                for model_name in sorted(all_models):
                    live = live_models.get(model_name) or {}
                    snap = snap_models.get(model_name) or {}
                    # Prefer live `mmx quota`, fall back to snapshot's
                    # `minimax_interval`, then to the legacy
                    # `providers.minimax.quota` field as a last resort.
                    rem_5h = (
                        live.get('remaining_percent')
                        if live.get('remaining_percent') is not None
                        else snap.get('remaining_percent')
                    )
                    status_5h = live.get('status') if live.get('status') is not None else snap.get('status')
                    resets_in_s_5h = live.get('resets_in_s') if live.get('resets_in_s') is not None else snap.get('resets_in_s')
                    if rem_5h is None:
                        # Last-resort fallback: the legacy stale field.
                        legacy = ((status.get('providers') or {}).get('minimax') or {}).get('quota', {}) or {}
                        legacy_model = (legacy.get('models') or {}).get(model_name) or {}
                        legacy_5h = legacy_model.get('window_5h') or {}
                        rem_5h = legacy_5h.get('remaining_percent')
                        if status_5h is None:
                            status_5h = legacy_5h.get('status')
                        if resets_in_s_5h is None:
                            resets_in_s_5h = legacy_5h.get('resets_in_s')
                    rem_week = live.get('weekly_remaining_percent') if live.get('weekly_remaining_percent') is not None else snap.get('weekly_remaining_percent')
                    windows.append({
                        'model': model_name,
                        'name': 'window_5h',
                        'remaining_percent': rem_5h,
                        'used_percent': (100 - rem_5h) if isinstance(rem_5h, (int, float)) else None,
                        'status': status_5h,
                        'resets_in_s': resets_in_s_5h,
                    })
                    windows.append({
                        'model': model_name,
                        'name': 'weekly',
                        'remaining_percent': rem_week,
                        'used_percent': (100 - rem_week) if isinstance(rem_week, (int, float)) else None,
                        'status': None,
                        'resets_in_s': None,
                    })
                # `fetched_at` = whichever source is freshest; live wins
                # by construction since we just called it.
                fetched_at = live_fetched_at or snap_fetched_at or datetime.datetime.now(timezone.utc).isoformat()
                payload = {
                    'cached': False,
                    'fetched_at': fetched_at,
                    'provider': 'minimax',
                    'windows': windows,
                    'summary': status.get('summary'),
                }
                _minimax_cache_set(payload)
                self.send_json(payload)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Moderation archive (for Banned/Timeout sub-tab). Reads
        # selena-project/data/moderation_actions_archive.jsonl directly.
        if path == '/api/moderation/archive':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = int(qs.get('limit', ['100'])[0])
            except ValueError:
                limit = 100
            limit = max(1, min(limit, 1000))
            action_filter_raw = qs.get('action_filter', [''])[0]
            if not action_filter_raw:
                # Default: both bans and timeouts (covers legacy and new action names)
                action_filter = {'ban', 'ban_user', 'timeout', 'timeout_user'}
            else:
                action_filter = set(a.strip() for a in action_filter_raw.split(',') if a.strip())
            archive_path = Path(SELENA_ROOT) / 'data' / 'moderation_actions_archive.jsonl'
            if not archive_path.exists():
                self.send_json({'count': 0, 'entries': []})
                return
            try:
                entries = []
                with open(archive_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if action_filter and e.get('action') not in action_filter:
                            continue
                        entries.append(e)
                # Sort by ts desc (newest first)
                entries.sort(key=lambda e: e.get('ts', ''), reverse=True)
                self.send_json({'count': len(entries), 'entries': entries[:limit]})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Pending actions list (dry-run / unexecuted moderation actions).
        if path == '/api/discord-lookup/pending' and self.command == 'GET':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'), 'pending', 'list'],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Add a pending action (does NOT execute). Body: {target_user_id, target_username, action, duration?, reason, source?, created_by?}
        if path == '/api/discord-lookup/pending' and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                body = json.loads(raw) if raw else {}
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json({'error': f'invalid JSON body: {e}'}, 400)
                return
            required = ['target_user_id', 'action', 'reason']
            for k in required:
                if not body.get(k):
                    self.send_json({'error': f'{k} required'}, 400)
                    return
            if body['action'] == 'timeout_user' and not body.get('duration'):
                self.send_json({'error': 'duration required for timeout_user'}, 400)
                return
            if body['action'] not in ('ban_user', 'timeout_user'):
                self.send_json({'error': "action must be 'ban_user' or 'timeout_user'"}, 400)
                return
            cmd = [
                'python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                'pending', 'add',
                '--target-user-id', str(body['target_user_id']),
                '--action', body['action'],
                '--reason', str(body['reason']),
            ]
            if body.get('target_username'):
                cmd += ['--target-username', str(body['target_username'])]
            if body.get('duration'):
                cmd += ['--duration', str(body['duration'])]
            if body.get('source'):
                cmd += ['--source', str(body['source'])]
            if body.get('created_by'):
                cmd += ['--created-by', str(body['created_by'])]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT))
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Edit a pending action (reason only). Body: {reason}
        # Path: /api/discord-lookup/pending/<id>
        if path.startswith('/api/discord-lookup/pending/') and self.command == 'PATCH':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            pending_id = path[len('/api/discord-lookup/pending/'):]
            if not pending_id:
                self.send_json({'error': 'id required in path'}, 400)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                body = json.loads(raw) if raw else {}
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json({'error': f'invalid JSON body: {e}'}, 400)
                return
            if not body.get('reason'):
                self.send_json({'error': 'reason required'}, 400)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'pending', 'edit', '--id', pending_id, '--reason', str(body['reason'])],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Delete a pending action (does NOT execute). Path: /api/discord-lookup/pending/<id>
        # NOTE: actual DELETE handler lives in do_DELETE() (added 2026-06-04).
        # The do_POST fallback was removed because BaseHTTPRequestHandler
        # was returning 501 for DELETE — dispatch by method is the only
        # way Python's http.server routes verbs.

        # Apply a single pending action. POST /api/discord-lookup/pending/<id>/apply
        if path.startswith('/api/discord-lookup/pending/') and path.endswith('/apply') and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            pending_id = path[len('/api/discord-lookup/pending/'):-len('/apply')]
            if not pending_id:
                self.send_json({'error': 'id required in path'}, 400)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'pending', 'apply', '--id', pending_id],
                    capture_output=True, text=True, timeout=180, cwd=str(SELENA_ROOT)
                )
                log_api('PENDING_APPLY', f'id={pending_id} exit={out.returncode}')
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Urgent lane: list /api/discord-lookup/pending-urgent
        if path == '/api/discord-lookup/pending-urgent' and self.command == 'GET':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'), 'pending', 'urgent-list'],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Urgent lane: add (does NOT execute — use cron for that)
        if path == '/api/discord-lookup/pending-urgent' and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                body = json.loads(raw) if raw else {}
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json({'error': f'invalid JSON body: {e}'}, 400)
                return
            required = ['target_user_id', 'action', 'reason']
            for k in required:
                if not body.get(k):
                    self.send_json({'error': f'{k} required'}, 400)
                    return
            cmd = [
                'python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                'pending', 'urgent-add',
                '--target-user-id', str(body['target_user_id']),
                '--action', body['action'],
                '--reason', str(body['reason']),
            ]
            for opt, key in (('--target-username', 'target_username'),
                             ('--duration', 'duration'),
                             ('--source', 'source'),
                             ('--created-by', 'created_by')):
                if body.get(key):
                    cmd += [opt, str(body[key])]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT))
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Urgent lane: clear one entry by id. DELETE /api/discord-lookup/pending-urgent/<id>
        # NOTE: actual DELETE handler lives in do_DELETE() (added 2026-06-04).

        # Apply ALL pending actions. POST /api/discord-lookup/pending/apply-all
        if path == '/api/discord-lookup/pending/apply-all' and self.command == 'POST':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'pending', 'apply', '--all'],
                    capture_output=True, text=True, timeout=600, cwd=str(SELENA_ROOT)
                )
                log_api('PENDING_APPLY_ALL', f'exit={out.returncode}')
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Action count for the moderation pipeline diagram (2026-06-04 per Arcurus).
        # Lightweight endpoint that just counts entries in
        # data/moderation_actions_archive.jsonl so the "execute" box on
        # the pipeline diagram can show "X done" without fetching the
        # whole archive.
        #
        # 2026-06-05 update: the "done" counter on the pipeline diagram
        # should match what the Banned/Timeout sub-tab shows, NOT the
        # raw total of all action records. The archive includes many
        # housekeeping actions (delete_message, send_to_review,
        # process_nudge, monitor, post_in_channel, etc.) that aren't
        # visible in the Banned/Timeout list. So we return BOTH the
        # raw total AND a banned_timeout_total that matches the sub-tab
        # filter. The JS uses banned_timeout_total for the displayed
        # counter and shows the breakdown in the hover tooltip.
        if path == '/api/moderation/actions/count' and self.command == 'GET':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                archive_path = os.path.join(SELENA_ROOT, 'data', 'moderation_actions_archive.jsonl')
                total = 0
                banned_timeout_total = 0
                today = 0
                last_24h = 0
                bans = 0
                timeouts = 0
                other = 0
                errors = 0
                # Matches the Banned/Timeout sub-tab filter in modLoadBanned()
                BANNED_TIMEOUT_ACTIONS = {'ban_user', 'ban', 'timeout_user', 'timeout'}
                if os.path.exists(archive_path):
                    now = datetime.datetime.now(timezone.utc)
                    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    last_24h_start = now - datetime.timedelta(hours=24)
                    with open(archive_path, encoding='utf-8') as f:
                        for line in f:
                            if not line.strip():
                                continue
                            try:
                                e = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            total += 1
                            act = e.get('action', '')
                            if act in BANNED_TIMEOUT_ACTIONS:
                                banned_timeout_total += 1
                                if act in ('ban_user', 'ban'):
                                    bans += 1
                                elif act in ('timeout_user', 'timeout'):
                                    timeouts += 1
                            else:
                                other += 1
                            if not e.get('result_ok', True):
                                errors += 1
                            ts = e.get('ts')
                            if ts:
                                try:
                                    t = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                    if t >= today_start:
                                        today += 1
                                    if t >= last_24h_start:
                                        last_24h += 1
                                except (ValueError, TypeError):
                                    pass
                self.send_json({
                    'total': total,                    # ALL action records
                    'banned_timeout_total': banned_timeout_total,  # what the Banned/Timeout sub-tab shows
                    'today': today,
                    'last_24h': last_24h,
                    'bans': bans,
                    'timeouts': timeouts,
                    'other': other,
                    'errors': errors,
                })
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Drift check: diff the cron prompt's policy block against policies.md
        if path == '/api/moderation/drift-check':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'), 'drift-check'],
                    capture_output=True, text=True, timeout=30, cwd=str(SELENA_ROOT)
                )
                if out.returncode not in (0, 1):
                    self.send_json({'error': f'drift-check CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Moderation policies (read-only display of moderation_policies.md)
        if path == '/api/moderation/policies':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            policies_path = Path(SELENA_ROOT) / 'data' / 'moderation_policies.md'
            if not policies_path.exists():
                self.send_json({'error': 'policies file not found', 'path': str(policies_path)})
                return
            try:
                stat = policies_path.stat()
                content = policies_path.read_text(encoding='utf-8')
                self.send_json({
                    'path': str(policies_path.relative_to(SELENA_ROOT)),
                    'last_updated': datetime.datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    'length': len(content),
                    'content': content,
                })
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Moderation architecture doc (docs/moderation.md)
        if path == '/api/moderation/docs':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            docs_path = Path(SELENA_ROOT) / 'docs' / 'moderation.md'
            if not docs_path.exists():
                self.send_json({'error': 'docs file not found', 'path': str(docs_path)})
                return
            try:
                stat = docs_path.stat()
                content = docs_path.read_text(encoding='utf-8')
                self.send_json({
                    'path': str(docs_path.relative_to(SELENA_ROOT)),
                    'last_updated': datetime.datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    'length': len(content),
                    'content': content,
                })
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Open World: entity property reference for the LLM narrator
        # (open-world-selena/ai_templates/property_docs.md, added
        # 2026-06-08 per Arcurus #openworld). Served as raw text
        # (not JSON-wrapped) so the link drops the user into a
        # readable browser view. Reads the SOURCE file directly
        # (single source of truth) — no copy in selena-project/.
        if path == '/api/openworld/property-docs':
            docs_path = Path(SELENA_ROOT).parent / 'open-world-selena' / 'ai_templates' / 'property_docs.md'
            if not docs_path.exists():
                self.send_json({'error': 'docs file not found', 'path': str(docs_path)}, 404)
                return
            try:
                content = docs_path.read_text(encoding='utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/markdown; charset=utf-8')
                self.send_header('Content-Length', str(len(content.encode('utf-8'))))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(content.encode('utf-8'))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Open World: entity property reference for the LLM narrator — JSON wrapper
        # (added 2026-06-08 per Arcurus #openworld). Returns the same file
        # wrapped in a JSON envelope with path / last_updated / length, for
        # programmatic clients (the web UI, the in-house archive). The
        # auth-gated mirror of /api/openworld/property-docs (which is
        # public-readable for browser convenience).
        if path == '/api/openworld/property-docs.json':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            docs_path = Path(SELENA_ROOT).parent / 'open-world-selena' / 'ai_templates' / 'property_docs.md'
            if not docs_path.exists():
                self.send_json({'error': 'docs file not found', 'path': str(docs_path)}, 404)
                return
            try:
                stat = docs_path.stat()
                content = docs_path.read_text(encoding='utf-8')
                self.send_json({
                    'path': str(docs_path.relative_to(Path(SELENA_ROOT).parent)),
                    'last_updated': datetime.datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    'length': len(content),
                    'content': content,
                })
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Knowledge Base endpoints
        if path == '/api/knowledge':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Get query params
            query = parse_qs(parsed.query)
            category = query.get('category', [None])[0] if 'category=' in parsed.query else None
            search = query.get('search', [None])[0] if 'search=' in parsed.query else None
            entries = kb.get_all_entries(category=category, search=search)
            categories = kb.get_categories()
            self.send_json({'entries': entries, 'categories': categories})
            return
        
        if path == '/api/knowledge/categories':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            categories = kb.get_categories()
            self.send_json({'categories': categories})
            return
        
        if path == '/api/knowledge/add':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            category = query.get('category', [''])[0]
            title = query.get('title', [''])[0]
            content = query.get('content', [''])[0]
            tags = query.get('tags', [''])[0].split(',') if 'tags=' in parsed.query else []
            if not category or not title:
                self.send_json({'success': False, 'error': 'category and title required'}, 400)
                return
            entry = kb.add_entry(category, title, content, tags)
            if entry:
                self.send_json({'success': True, 'entry': entry})
            else:
                self.send_json({'success': False, 'error': 'Invalid category or save failed'}, 400)
            return
        
        if path == '/api/knowledge/delete':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            entry_id = query.get('id', [''])[0]
            if not entry_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            deleted = kb.delete_entry(entry_id)
            self.send_json({'success': deleted})
            return
        
        if path == '/api/knowledge/update':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            entry_id = query.get('id', [''])[0]
            if not entry_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            # Extract update fields
            title = query.get('title', [None])[0] if 'title=' in parsed.query else None
            content = query.get('content', [None])[0] if 'content=' in parsed.query else None
            category = query.get('category', [None])[0] if 'category=' in parsed.query else None
            tags = query.get('tags', [''])[0].split(',') if 'tags=' in parsed.query else None
            entry = kb.update_entry(entry_id, title=title, content=content, tags=tags, category=category)
            if entry:
                self.send_json({'success': True, 'entry': entry})
            else:
                self.send_json({'success': False, 'error': 'Entry not found or update failed'}, 404)
            return
        
        # Projects endpoints
        if path == '/api/projects':
            # Auth required (added 2026-06-04 per Arcurus). The previous
            # "no auth required" comment predated the password rule; project
            # metadata is internal info and should be auth-gated.
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Load projects from file
            projects_file = os.path.join(SELENA_ROOT, 'docs', 'projects.md')
            projects = []
            if os.path.exists(projects_file):
                # Parse markdown for projects (simple format)
                try:
                    with open(projects_file, 'r') as f:
                        content = f.read()
                    # Simple project extraction
                    import re
                    # Find project names under ### headers
                    headers = re.findall(r'^###\s+(.+)$', content, re.MULTILINE)
                    current_project = None
                    for line in content.split('\n'):
                        if line.startswith('### '):
                            current_project = line[4].strip()
                        elif current_project and line.strip().startswith('- **') and ':' in line:
                            key_match = re.search(r'\*\*(.+?)\*\*:\s*(.+)', line)
                            if key_match:
                                key = key_match.group(1).lower().replace(' ', '_')
                                value = key_match.group(2).strip()
                                if key == 'name':
                                    projects.append({'name': value, 'description': '', 'status': 'active', 'port': '', 'repo': ''})
                                elif len(projects) > 0:
                                    if key == 'description':
                                        projects[-1]['description'] = value
                                    elif key == 'status':
                                        projects[-1]['status'] = value
                                    elif key == 'port':
                                        projects[-1]['port'] = value
                                    elif key == 'repo':
                                        projects[-1]['repo'] = value
                                    elif key == 'directory':
                                        projects[-1]['directory'] = value
                                    elif key == 'parentproject':
                                        projects[-1]['parentProject'] = value
                except Exception as e:
                    pass

            # Decorate each project with parentProject / is_parent /
            # children from the project_mapping.json (the canonical source
            # of truth for project definitions). Per Arcurus 2026-06-09
            # #cost-tracker: project_mapping.json is now the schema-v2
            # store with parentProject / children fields. The markdown
            # file remains the human-friendly description, but the
            # parent/child metadata comes from the JSON.
            project_defs = _load_project_defs()
            for p in projects:
                slug = p.get('name')
                if not slug:
                    continue
                d = project_defs.get(slug, {})
                p['parentProject'] = p.get('parentProject') or d.get('parentProject')
                p['is_parent'] = bool(d.get('is_parent'))
                p['children'] = d.get('children') or []
                p['emoji'] = d.get('emoji', p.get('emoji', ''))
                p['color'] = d.get('color', p.get('color', ''))
                p['worker_cron_id'] = d.get('worker_cron_id')
                p['worker_cron_name'] = d.get('worker_cron_name')
                p['primary_channel_id'] = d.get('primary_channel_id')

            # Add default projects if none found
            if not projects:
                projects = [
                    {'name': 'open-world-selena', 'description': 'Rust-based evolving world with LLM-driven entities', 'status': 'active', 'port': '8081', 'repo': 'https://github.com/Arcurus/openworld-selena'},
                    {'name': 'selena-project', 'description': 'Self-development, memory, reflection system', 'status': 'active', 'port': '8765', 'repo': 'https://github.com/Arcurus/selena'}
                ]

            self.send_json({'projects': projects})
            return

        # /api/projects/tree — return projects grouped by parent so the
        # UI can render a tree (added 2026-06-09 per Arcurus #cost-tracker).
        # Each top-level entry is either a parent (with `children` array)
        # or a top-level project (with empty `children`). The parent's
        # `children` are full project objects so the UI doesn't need a
        # second fetch.
        if path == '/api/projects/tree':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            project_defs = _load_project_defs()
            # Get the basic list first (so we have description, status, port, repo)
            # by delegating to the markdown parser. Easier: just inline the
            # parser. We keep this DRY by reusing the same logic.
            projects_file = os.path.join(SELENA_ROOT, 'docs', 'projects.md')
            base = []
            if os.path.exists(projects_file):
                try:
                    with open(projects_file, 'r') as f:
                        content = f.read()
                    import re
                    current_project = None
                    for line in content.split('\n'):
                        if line.startswith('### '):
                            current_project = line[4].strip()
                        elif current_project and line.strip().startswith('- **') and ':' in line:
                            key_match = re.search(r'\*\*(.+?)\*\*:\s*(.+)', line)
                            if key_match:
                                key = key_match.group(1).lower().replace(' ', '_')
                                value = key_match.group(2).strip()
                                if key == 'name':
                                    base.append({'name': value, 'description': '', 'status': 'active', 'port': '', 'repo': ''})
                                elif len(base) > 0:
                                    if key == 'description': base[-1]['description'] = value
                                    elif key == 'status': base[-1]['status'] = value
                                    elif key == 'port': base[-1]['port'] = value
                                    elif key == 'repo': base[-1]['repo'] = value
                                    elif key == 'directory': base[-1]['directory'] = value
                except Exception:
                    pass
            # Decorate and group
            by_slug = {}
            for p in base:
                slug = p.get('name')
                d = project_defs.get(slug, {})
                p['parentProject'] = d.get('parentProject')
                p['is_parent'] = bool(d.get('is_parent'))
                p['children_slugs'] = d.get('children') or []
                p['emoji'] = d.get('emoji', '')
                p['color'] = d.get('color', '')
                p['worker_cron_id'] = d.get('worker_cron_id')
                p['worker_cron_name'] = d.get('worker_cron_name')
                p['primary_channel_id'] = d.get('primary_channel_id')
                by_slug[slug] = p
            # Group: top-level = parents OR orphans (no parent slug in
            # the loaded set). Each parent's `children` is a list of
            # full child objects.
            tree = []
            children_lookup: dict = {}
            for slug, p in by_slug.items():
                parent = p.get('parentProject')
                if parent and parent in by_slug:
                    children_lookup.setdefault(parent, []).append(p)
            for slug, p in by_slug.items():
                if p.get('is_parent') or not p.get('parentProject'):
                    p['children'] = children_lookup.get(slug, [])
                    tree.append(p)
                # (children are nested under their parent; don't add as a
                #  separate top-level entry)
            self.send_json({'tree': tree, 'flat': list(by_slug.values())})
            return
        
        # Service management endpoints
        if path == '/api/services/list':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Return known services with their status
            services = []
            # Check API server
            api_pid = get_pid_file(os.path.join(DATA_DIR, 'api_server.pid'))
            services.append({
                'name': 'selena-api',
                'description': 'Selena v2 API Server',
                'port': 8765,
                'pid': api_pid,
                'running': check_process_running(api_pid),
                'start_command': 'cd {} && nohup python3 code/api_server.py > /tmp/api_server.log 2>&1 &'.format(SELENA_ROOT)
            })
            # Check Open World server
            ow_pid = get_pid_file(os.path.join(DATA_DIR, 'open_world.pid'))
            services.append({
                'name': 'open-world-selena',
                'description': 'Open World Rust Server',
                'port': 8081,
                'pid': ow_pid,
                'running': check_process_running(ow_pid),
                'start_command': 'cd {} && nohup cargo run > /tmp/open_world.log 2>&1 &'.format(os.path.join(SELENA_ROOT, '..', 'open-world-selena'))
            })
            self.send_json({'services': services})
            return
        
        # Server-side health check endpoint - avoids CORS issues
        if path.startswith('/api/services/check'):
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query) if parsed.query else {}
            service_name = query.get('service', [''])[0] if query else ''
            
            # For selena-api, return ok if this server is responding (can't self-check via HTTP)
            if service_name == 'selena-api':
                # If we're handling this request, the server is running
                self.send_json({'service': service_name, 'status': 'ok', 'statusText': 'Running'})
                return
            
            # For agent-system, return ok since selena-api is running (agent system is part of it)
            if service_name == 'agent-system':
                self.send_json({'service': service_name, 'status': 'ok', 'statusText': 'Running'})
                return
            
            # Special case: in-process sub-services that can't self-HTTP (single-threaded http.server).
            # Check the notifier state directly so the services panel shows real status.
            if service_name == 'selena-discord-notifier':
                try:
                    n = get_default_notifier()
                    st = n.status()
                    enabled = bool(st.get('enabled'))
                    if enabled:
                        posts = st.get('post_count', 0)
                        err = st.get('last_error')
                        if err:
                            self.send_json({'service': service_name, 'status': 'ok', 'statusText': f'Running (posts={posts}, last_error={err[:40]})'})
                        else:
                            self.send_json({'service': service_name, 'status': 'ok', 'statusText': f'Running (posts={posts})'})
                    else:
                        self.send_json({'service': service_name, 'status': 'offline', 'statusText': 'Notifier disabled'})
                except Exception as e:
                    self.send_json({'service': service_name, 'status': 'offline', 'statusText': 'Notifier check failed', 'error': str(e)})
                return

            # For other services, make HTTP check
            check_urls = {
                'open-world-selena': 'http://localhost:8081/',
                'openclaw-gateway': 'http://localhost:18789/',
            }

            if service_name not in check_urls:
                self.send_json({'error': f'Unknown service: {service_name}'}, 400)
                return

            url = check_urls[service_name]

            # Do server-side HTTP check
            import urllib.request
            try:
                req = urllib.request.Request(url, method='GET')
                req.add_header('User-Agent', 'Selena-API-Check/1.0')
                urllib.request.urlopen(req, timeout=3)
                self.send_json({'service': service_name, 'status': 'ok', 'statusText': 'Running'})
            except urllib.error.HTTPError as e:
                # HTTP error but service is responding
                self.send_json({'service': service_name, 'status': 'ok', 'statusText': 'Running', 'httpCode': e.code})
            except Exception as e:
                self.send_json({'service': service_name, 'status': 'offline', 'statusText': 'Offline', 'error': str(e)})
            return

        # ----- Watchdog / auto-start config (driven by docs/projects.md) -----
        if path == '/api/services/config':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            services = sm_load_services()
            state = sm_load_state()
            # Enrich with live health
            for s in services:
                name = s.get('name')
                if name in ('selena-project',):
                    s['live_status'] = 'online'
                    s['live_detail'] = 'self-check'
                else:
                    up, detail = sm_check_health(s)
                    s['live_status'] = 'online' if up else 'offline'
                    s['live_detail'] = detail
                s['watched'] = sm_is_monitored(name)
                s_state = state.get(name, {})
                s['last_seen'] = s_state.get('last_seen')
                s['last_status'] = s_state.get('last_status')
                s['last_skip_reason'] = s_state.get('last_skip_reason')
            self.send_json({
                'services': services,
                'monitor': service_monitor.status(),
            })
            return

        if path == '/api/services/monitor/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            services = sm_load_services()
            state = sm_load_state()
            entries = []
            for s in services:
                name = s.get('name')
                if not sm_is_monitored(name):
                    continue
                if name in ('selena-project',):
                    up, detail = True, 'self-check'
                else:
                    up, detail = sm_check_health(s)
                s_state = state.get(name, {})
                entries.append({
                    'name': name,
                    'description': s.get('description', ''),
                    'port': s.get('port'),
                    'up': up,
                    'detail': detail,
                    'auto_start': s.get('auto_start'),
                    'enabled': s.get('enabled'),
                    'check_method': s.get('check_method'),
                    'start_command': s.get('start_command'),
                    'grace_period_seconds': s.get('grace_period_seconds', 20),
                    'max_restarts_per_hour': s.get('max_restarts_per_hour', 5),
                    'last_seen': s_state.get('last_seen'),
                    'last_status': s_state.get('last_status'),
                    'last_skip_reason': s_state.get('last_skip_reason'),
                })
            self.send_json({
                'monitor': service_monitor.status(),
                'services': entries,
            })
            return

        if path == '/api/services/monitor/restarts':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            history = sm_load_restart_history()
            query = parse_qs(parsed.query) if parsed.query else {}
            limit = int(query.get('limit', ['50'])[0])
            self.send_json({
                'restarts': history[-limit:][::-1],
                'total': len(history),
            })
            return

        if path == '/api/services/monitor/check':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Run a single watchdog cycle synchronously
            actions = check_and_restart_cycle()
            self.send_json({
                'success': True,
                'actions': actions,
                'monitor': service_monitor.status(),
            })
            return

        if path == '/api/services/auto_start':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query) if parsed.query else {}
            name = query.get('name', [''])[0]
            if not name:
                self.send_json({'success': False, 'error': 'name required'}, 400)
                return
            updates = {}
            if 'enabled' in query:
                updates['enabled'] = query['enabled'][0].lower() in ('1', 'true', 'yes')
            if 'auto_start' in query:
                updates['auto_start'] = query['auto_start'][0].lower() in ('1', 'true', 'yes')
            if 'start_command' in query:
                updates['start_command'] = query['start_command'][0]
            if 'check_method' in query:
                updates['check_method'] = query['check_method'][0]
            if 'health_url' in query:
                updates['health_url'] = query['health_url'][0]
            if 'grace_period_seconds' in query:
                updates['grace_period_seconds'] = query['grace_period_seconds'][0]
            if 'max_restarts_per_hour' in query:
                updates['max_restarts_per_hour'] = query['max_restarts_per_hour'][0]
            ok, msg = sm_update_service(name, updates)
            self.send_json({'success': ok, 'message': msg, 'updated': updates})
            return

        if path == '/api/services/monitor/announcements':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Drain pending announcements (used by the bot to push to #selena-project)
            items = service_monitor.drain_announcements()
            self.send_json({'announcements': items, 'count': len(items)})
            return

        # ----- Discord notifier (direct integration, no LLM) -----
        if path == '/api/discord/status':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json({
                'notifier': get_default_notifier().status(),
                'cron_announcer': 'a6d79a91-107f-4343-a479-880c407c8045 (still registered but redundant; can be disabled)',
            })
            return

        if path == '/api/discord/test':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query) if parsed.query else {}
            text = query.get('text', [None])[0]
            if not text:
                text = '🧪 selena-project direct Discord test (manual)'
            n = get_default_notifier()
            if not n.enabled:
                n.start()
            if not n.enabled:
                self.send_json({'success': False, 'error': 'notifier disabled (token missing or DISCORD_ENABLED=false)'}, 400)
                return
            channel = query.get('channel', [None])[0]
            ok = n.send_message(channel, text)
            self.send_json({'success': ok, 'channel': channel or n.default_channel_id, 'text': text[:200], 'status': n.status()})
            return

        if path == '/api/discord/restart':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            n = get_default_notifier()
            n.stop()
            ok = n.start()
            self.send_json({'success': ok, 'status': n.status()})
            return

        # Public health check (no auth) — used by the watchdog via check_method: http
        # NOTE (2026-06-04 per Arcurus): the watchdog actually does an in-process
        # check via a special case in service_manager.check_health() and does NOT
        # call this endpoint. The canonical CLI is
        #   `python3 scripts/discord_lookup.py discord-health`
        # which returns 0/1/2. This HTTP endpoint is kept for external monitors
        # and now requires auth per the "all APIs require a password" rule.
        if path == '/api/discord/health':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            n = get_default_notifier()
            st = n.status()
            healthy = bool(st.get('enabled'))
            payload = {
                'service': 'selena-discord-notifier',
                'healthy': healthy,
                'notifier': st,
            }
            if healthy:
                self.send_json(payload, 200)
            else:
                # Hint why we're down so the watchdog log is useful
                payload['hint'] = (
                    'notifier disabled (token missing/revoked, or DISCORD_ENABLED=false). '
                    'Check /api/discord/status with auth for details.'
                )
                self.send_json(payload, 503)
            return

        if path == '/api/services/restart' or path == '/api/services/stop':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            
            # Parse query string from the full URL (self.path includes query)
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query) if parsed.query else {}
            
            service_name = query.get('service', [''])[0] if query else ''
            if not service_name:
                self.send_json({'success': False, 'error': 'service name required'}, 400)
                return
            
            result = {'success': False, 'service': service_name, 'action': 'stop' if path == '/api/services/stop' else 'restart'}
            
            if path == '/api/services/stop':
                # Stop the service
                if service_name == 'selena-api':
                    pid_file = os.path.join(DATA_DIR, 'api_server.pid')
                    if os.path.exists(pid_file):
                        with open(pid_file, 'r') as f:
                            pid = int(f.read().strip())
                        try:
                            os.kill(pid, 9)
                            result['success'] = True
                            result['message'] = f'Stopped selena-api (PID {pid})'
                        except ProcessLookupError:
                            result['success'] = True
                            result['message'] = f'Process {pid} already gone'
                        except Exception as e:
                            result['error'] = str(e)
                    else:
                        result['error'] = 'No PID file found'
                elif service_name == 'open-world-selena':
                    # Try PID file first, then check port
                    pid_file = os.path.join(DATA_DIR, 'open_world.pid')
                    pid_killed = False
                    if os.path.exists(pid_file):
                        with open(pid_file, 'r') as f:
                            pid = int(f.read().strip())
                        try:
                            os.kill(pid, 9)
                            pid_killed = True
                            result['message'] = f'Stopped open-world-selena (PID {pid})'
                        except ProcessLookupError:
                            result['message'] = f'PID {pid} already gone, checking port...'
                        except Exception as e:
                            result['error'] = str(e)
                    
                    # Also check if port 8081 is still in use and kill that process
                    try:
                        # Find process using port 8081
                        check = subprocess.run(['fuser', '8081/tcp'], capture_output=True, text=True)
                        if check.stdout.strip():
                            pid_from_port = int(check.stdout.strip().split()[0])
                            try:
                                os.kill(pid_from_port, 9)
                                result['message'] = f'Stopped open-world-selena (port PID {pid_from_port})'
                                pid_killed = True
                            except:
                                pass
                    except:
                        pass
                    
                    if pid_killed or 'port' in result.get('message', ''):
                        result['success'] = True
                    elif 'error' not in result:
                        result['error'] = 'No PID file and port 8081 not in use'
                else:
                    result['error'] = f'Unknown service: {service_name}'
            else:
                # Restart = stop + start
                if service_name == 'selena-api':
                    # Stop first
                    pid_file = os.path.join(DATA_DIR, 'api_server.pid')
                    if os.path.exists(pid_file):
                        with open(pid_file, 'r') as f:
                            pid = int(f.read().strip())
                        try:
                            os.kill(pid, 9)
                        except:
                            pass
                    # Start
                    time.sleep(1)
                    start_cmd = 'cd {} && nohup python3 code/api_server.py > /tmp/api_server.log 2>&1 &'.format(SELENA_ROOT)
                    os.system(start_cmd)
                    time.sleep(2)
                    new_pid = get_pid_for_command('python3', 'api_server.py')
                    if new_pid:
                        with open(pid_file, 'w') as f:
                            f.write(str(new_pid))
                    result['success'] = True
                    result['message'] = f'Restarted selena-api (new PID {new_pid})'
                elif service_name == 'open-world-selena':
                    # Stop first
                    pid_file = os.path.join(DATA_DIR, 'open_world.pid')
                    if os.path.exists(pid_file):
                        with open(pid_file, 'r') as f:
                            pid = int(f.read().strip())
                        try:
                            os.kill(pid, 9)
                        except:
                            pass
                    # Start
                    time.sleep(1)
                    ow_dir = os.path.join(SELENA_ROOT, '..', 'open-world-selena')
                    start_cmd = 'cd {} && nohup cargo run > /tmp/open_world.log 2>&1 &'.format(ow_dir)
                    os.system(start_cmd)
                    time.sleep(3)
                    new_pid = get_pid_for_command('cargo', 'run')
                    if new_pid:
                        with open(pid_file, 'w') as f:
                            f.write(str(new_pid))
                    result['success'] = True
                    result['message'] = f'Restarted open-world-selena (new PID {new_pid})'
                else:
                    result['error'] = f'Unknown service: {service_name}'
            
            # Log the action
            if result['success']:
                log_activity(f"{result['action'].capitalize()} {service_name}: {result.get('message', 'OK')}", 'success')
            else:
                log_activity(f"Failed to {result['action']} {service_name}: {result.get('error', 'Unknown error')}", 'error')
            
            self.send_json(result)
            return
        
        # Cost tracking endpoints
        if path == '/api/cost/tracking':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            
            # Load cost tracking from file
            cost_file = os.path.join(DATA_DIR, 'cost_tracking.json')
            cost_data = {
                'tokenPlan': {
                    'name': 'MiniMax Plus',
                    'totalCalls': 4500,
                    'usedCalls': 0,
                    'leftCalls': 4500,
                    'history': []
                },
                'calls': []
            }
            
            if os.path.exists(cost_file):
                try:
                    with open(cost_file, 'r') as f:
                        cost_data = json.load(f)
                except:
                    pass
            
            self.send_json(cost_data)
            return
        
        # Activity log endpoint
        if path == '/api/activity/log':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Return recent activity log entries
            limit = 50  # Default limit
            if '?' in path:
                query_params = parse_qs(path.split('?')[1])
                limit = int(query_params.get('limit', [50])[0])
            entries = activity_log[-limit:] if len(activity_log) > 0 else []
            self.send_json({'activities': entries, 'total': len(activity_log)})
            return
        
        if path == '/api/activity/errors':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Return only error entries
            errors = [a for a in activity_log if a.get('type') == 'error']
            self.send_json({'errors': errors, 'total': len(errors)})
            return
        
        if path == '/api/relations':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json({'relations': get_memory_relations()})
            return
        
        # Serve static files from web directory
        if path == '/' or path == '/index.html':
            web_path = os.path.join(SELENA_ROOT, 'web', 'index.html')
            if os.path.exists(web_path):
                with open(web_path, 'r') as f:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                    self.send_header('Pragma', 'no-cache')
                    self.send_header('Expires', '0')
                    self.send_cors_headers()
                    self.end_headers()
                    self.wfile.write(f.read().encode('utf-8'))
            else:
                self.send_html('<html><body><h1>Web interface not found</h1></body></html>', 404)
            return
        
        # Browser auto-fallback for favicon (2026-06-11).
        # Browsers auto-request /favicon.ico from the page's directory
        # BEFORE the page parses, so we serve our 32x32 PNG here even
        # though the dashboard is at /selena-astra/* and Caddy strips
        # the prefix. Without this alias, the tab shows a generic
        # "broken icon" until the page's <link rel="icon"> tags load.
        if path == '/favicon.ico':
            favicon_path = os.path.join(SELENA_ROOT, 'web', 'img',
                                        'favicon-32x32.png')
            if os.path.exists(favicon_path):
                with open(favicon_path, 'rb') as f:
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/x-icon')
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.send_cors_headers()
                    self.end_headers()
                    self.wfile.write(f.read())
                return
            # fall through to 404 if the PNG is missing

        # Serve other static files
        if path.startswith('/static/'):
            # Extract and sanitize the requested file path
            requested_name = path[8:]  # Remove '/static/'
            
            # Block path traversal attempts - reject any path containing '..'
            if '..' in requested_name:
                self.send_json({'error': 'Forbidden: Path traversal not allowed'}, 403)
                return
            
            # Build full path and verify it stays within web directory
            web_dir = os.path.join(SELENA_ROOT, 'web')
            file_path = os.path.join(web_dir, requested_name)
            
            # Resolve to real path and verify it's within web directory
            real_path = os.path.realpath(file_path)
            real_web_dir = os.path.realpath(web_dir)
            
            if not real_path.startswith(real_web_dir + os.sep):
                self.send_json({'error': 'Forbidden: Access denied'}, 403)
                return
            
            if os.path.exists(file_path):
                with open(file_path, 'rb') as f:
                    content = f.read()
                
                if file_path.endswith('.css'):
                    content_type = 'text/css'
                elif file_path.endswith('.js'):
                    content_type = 'application/javascript'
                elif file_path.endswith('.html') or file_path.endswith('.htm'):
                    content_type = 'text/html; charset=utf-8'
                elif file_path.endswith('.png'):
                    content_type = 'image/png'
                elif file_path.endswith('.svg'):
                    # Browsers refuse SVGs with the wrong MIME (Chrome shows
                    # nothing, Firefox warns). 2026-06-11: the favicon work
                    # exposed this gap — the static handler used to fall
                    # back to text/plain, which made the moon favicon
                    # invisible. Added so the SVG variant of the favicon
                    # (and any future SVGs) render correctly.
                    content_type = 'image/svg+xml'
                elif file_path.endswith('.ico'):
                    # Browsers auto-request /favicon.ico even when an SVG
                    # favicon is declared. Serve it with the right MIME so
                    # the favicon shows up on the first paint (before the
                    # page's <link rel="icon"> tags are parsed).
                    content_type = 'image/x-icon'
                elif file_path.endswith('.jpg') or file_path.endswith('.jpeg'):
                    content_type = 'image/jpeg'
                elif file_path.endswith('.webp'):
                    content_type = 'image/webp'
                elif file_path.endswith('.gif'):
                    content_type = 'image/gif'
                elif file_path.endswith('.mp4'):
                    # Login-form moon video (2026-06-11, per Arcurus
                    # #selena-project-important "please use in the login
                    # form also the same video as you used in
                    # selenaastra.com"). Without the right MIME, the
                    # browser refuses to play it.
                    content_type = 'video/mp4'
                elif file_path.endswith('.webm'):
                    content_type = 'video/webm'
                else:
                    content_type = 'text/plain'
                
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_html('<html><body><h1>File not found</h1></body></html>', 404)
            return
        
        # 404 for everything else
        self.send_html('<html><body><h1>Not found</h1></body></html>', 404)

    def do_POST(self):
        """Handle POST requests - currently supports /api/todos/add"""
        parsed = urlparse(self.path)
        path = parsed.path

        # Discord Lookup — manual scan (Run Now button)
        if path == '/api/discord-lookup/scan':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'scan', '--manual'],
                    capture_output=True, text=True, timeout=120, cwd=str(SELENA_ROOT)
                )
                log_api('DISCORD_LOOKUP_SCAN_MANUAL', f'exit={out.returncode}')
                if out.returncode != 0 and not out.stdout:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                # The CLI exits 1 on error decision, 0 otherwise. Both are valid.
                self.send_json(json.loads(out.stdout) if out.stdout else {'error': 'no stdout'})
            except subprocess.TimeoutExpired:
                self.send_json({'error': 'scan timed out after 120s'}, 504)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Discord Lookup — force-trigger moderation cron (bypasses debounce)
        if path == '/api/discord-lookup/trigger-cron':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                job_id = '1b0f1a2b-5677-4e8e-9699-17c29e55014c'
                out = subprocess.run(
                    ['openclaw', 'cron', 'run', job_id],
                    capture_output=True, text=True, timeout=30
                )
                log_api('DISCORD_LOOKUP_FORCE_TRIGGER', f'exit={out.returncode}')
                if out.returncode != 0:
                    self.send_json({'error': f'openclaw cron run exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                # Update last_wake_at in state
                state_path = Path(SELENA_ROOT) / 'data' / 'moderation_state' / 'discord_lookup_state.json'
                if state_path.exists():
                    st = json.loads(state_path.read_text())
                    st['last_wake_at'] = datetime.datetime.now(timezone.utc).isoformat()
                    st['wake_count'] = st.get('wake_count', 0) + 1
                    state_path.write_text(json.dumps(st, indent=2, ensure_ascii=False))
                self.send_json({'success': True, 'stdout': out.stdout.strip()})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # Discord Lookup — settings update via POST (used when PATCH isn't available)
        if path == '/api/discord-lookup/settings':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                body = json.loads(raw) if raw else {}
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json({'error': f'invalid JSON body: {e}'}, 400)
                return
            if not isinstance(body, dict) or not body:
                self.send_json({'error': 'body must be a non-empty JSON object of {key: value}'}, 400)
                return
            results = []
            for k, v in body.items():
                if isinstance(v, (list, dict)):
                    val_str = json.dumps(v)
                elif isinstance(v, bool):
                    val_str = 'true' if v else 'false'
                else:
                    val_str = str(v)
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'settings', 'set', '--key', k, '--value', val_str],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                results.append({'key': k, 'ok': out.returncode == 0, 'stdout': out.stdout.strip()[:200],
                                'stderr': out.stderr.strip()[:200] if out.returncode != 0 else None})
            log_api('DISCORD_LOOKUP_SETTINGS', f'updated {len(body)} key(s)')
            self.send_json({'success': all(r['ok'] for r in results), 'results': results})
            return

        # Handle CORS preflight

        # ── POST /api/todos/{id}/done ──
        # REST-style mark-done endpoint. Added 2026-06-05 per
        # selena-project-worker to address loose-end todo f2a6f57b
        # ("Selena v2 API todo endpoint HTTP methods are non-REST"):
        # the legacy GET /api/todos/mark-done is kept for backward
        # compat, but new callers should use POST /api/todos/{id}/done.
        # Body (optional): {"what_happened": "..."} — same field as the
        # legacy endpoint, passed through to todo_manager.mark_done.
        if path.startswith('/api/todos/') and path.endswith('/done') and path.count('/') == 4:
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            todo_id = path[len('/api/todos/'):-len('/done')]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required in path'}, 400)
                return
            # Optional body for what_happened; tolerate empty/no body.
            what_happened = None
            try:
                content_length = int(self.headers.get('Content-Length', '0'))
                if content_length:
                    raw = self.rfile.read(content_length).decode('utf-8') or '{}'
                    body = json.loads(raw)
                    if isinstance(body, dict):
                        what_happened = body.get('what_happened')
            except (ValueError, json.JSONDecodeError):
                what_happened = None
            todo = todo_manager.mark_done(todo_id, what_happened=what_happened)
            if todo:
                self.send_json({'success': True, 'todo': todo, 'endpoint': 'POST /api/todos/{id}/done'})
            else:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
            return

        if path == '/api/todos/add':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return

            # Read JSON body
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_json({'success': False, 'error': 'Request body is empty'}, 400)
                return
            
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
            except json.JSONDecodeError:
                self.send_json({'success': False, 'error': 'Invalid JSON'}, 400)
                return
            
            # Extract todo fields
            short_desc = data.get('short_desc', '')
            long_desc = data.get('long_desc', '')
            priority = int(data.get('priority', 5))
            sensitive = data.get('sensitive', False)
            parent_id = data.get('parent_id')
            estimated_llm_calls = data.get('estimated_llm_calls')
            creator_id = data.get('creator_id')
            conversation_id = data.get('conversation_id')
            agent_id = data.get('agent_id')
            project = data.get('project')
            agent_owner = data.get('agent_owner')
            what_happened = data.get('what_happened')
            # NEW (2026-06-08): irreversible / block_reason per
            # Arcurus #openworld.  The 1st /api/todos/add endpoint
            # (at line 1845) also reads these from query/body and
            # forwards them to add_todo — this 2nd endpoint (the
            # POST /api/todos/add that wraps mark_done + add in one
            # path) needs the same forwarding or add_todo raises
            # NameError on the new args.
            irreversible = (None if "irreversible" not in data else bool(data["irreversible"]))
            block_reason = data.get('block_reason')

            if not short_desc:
                self.send_json({'success': False, 'error': 'short_desc required'}, 400)
                return

            # Defensive dedup (added 2026-06-03 — mirror of the GET handler):
            # if short_desc + creator_id match an existing open todo, return
            # the existing record instead of creating a second one.
            if creator_id and short_desc:
                existing = todo_manager.find_open_by_signature(short_desc, creator_id)
                if existing:
                    log_api('TODO_DUP_SKIP', f'POST skipped duplicate add for {creator_id}: {short_desc[:60]}')
                    self.send_json({'success': True, 'todo': existing, 'deduped': True})
                    return

            # Parse force flag (bypass semantic dedup) — mirror the
            # _dispatch_routes /api/todos/add handler so the web UI's
            # addTodo() flow can opt out of dedup the same way.
            force = False
            if isinstance(data, dict) and 'force' in data:
                force = bool(data['force'])
            try:
                result = todo_manager.add_todo(short_desc, long_desc, priority, sensitive, parent_id, estimated_llm_calls, creator_id, conversation_id, agent_id, project, agent_owner, what_happened, irreversible, block_reason, force=force)
                # Flatten the manager's return shape to match the
                # _dispatch_routes /api/todos/add response (v0.8): the
                # web UI reads `dedup_action`, `similar`, and
                # `force_offered` as siblings of `todo`, not nested
                # inside it.
                self.send_json({
                    'success': True,
                    'todo': result.get('todo'),
                    'similar': result.get('similar', []),
                    'deduped': result.get('deduped'),
                    'dedup_action': result.get('dedup_action'),
                    'force_offered': result.get('force_offered', False),
                    'embedding_saved': result.get('embedding_saved', False),
                })
            except TodoDuplicateError as dup:
                # v0.8 dedup bands: same as the _dispatch_routes handler.
                # Returns band + force_offered so the web UI can decide
                # between "edit/use/force" (conflict) and "already added"
                # (duplicate, no force escape).  The 409 status is the
                # same in both cases.
                log_api(
                    'TODO_SEMANTIC_CONFLICT',
                    f'POST band={"duplicate" if not dup.force_offered else "conflict"} '
                    f'for {short_desc[:60]} with {dup.existing.get("id")} '
                    f'score={dup.existing.get("_score")}'
                )
                self.send_json({
                    'success': False,
                    'error': 'semantic duplicate',
                    'band': 'duplicate' if not dup.force_offered else 'conflict',
                    'force_offered': dup.force_offered,
                    'existing': dup.existing,
                    'similar': dup.similar,
                }, 409)
            return

        if path == '/api/world/scheduler/config':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length) if content_length else b'{}'
                payload = json.loads(body.decode('utf-8') or '{}')
            except json.JSONDecodeError as e:
                self.send_json({'success': False, 'error': f'Invalid JSON: {e}'}, 400)
                return
            from scheduled_actions import save_scheduler_config
            cfg = save_scheduler_config(payload, updated_by='api')
            # If a cycle is mid-sleep, the new interval is picked up on
            # the next loop iteration (max 5s of latency).
            self.send_json({
                'success': True,
                'message': 'Scheduler config updated',
                'config': cfg,
                'status': scheduler.status(),
            })
            return

        if path == '/api/llm-usage/record':
            # Accept either a user session (Bearer / cookie) OR the static
            # service token.  Service token is what the Open World Rust
            # server and scheduled_actions.py use.
            authed = self.authenticate()
            if not authed and LLM_RECORD_TOKEN:
                hdr = self.headers.get('Authorization', '')
                if hdr.startswith('Bearer '):
                    authed = (hdr[7:] == LLM_RECORD_TOKEN)
            if not authed:
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            provider = qs.get('provider', [''])[0]
            model = qs.get('model', [''])[0]
            project = qs.get('project', [''])[0]
            ti = qs.get('tokens_in', [None])[0]
            to = qs.get('tokens_out', [None])[0]
            rt = qs.get('reasoning_tokens', [None])[0]
            ci = qs.get('chars_in', [None])[0]
            co = qs.get('chars_out', [None])[0]
            cr = qs.get('chars_reasoning', [None])[0]
            # Optional session/message ids for dedup against the
            # reconciler's events (added 2026-06-08). Without these
            # the record is in-memory only (no file write, no double-
            # tracking risk).
            sid = qs.get('session_id', [None])[0]
            mid = qs.get('message_id', [None])[0]
            if not provider or not model:
                self.send_json({'error': 'provider and model required'}, 400)
                return
            t = _get_llm_tracker()
            result = t.record(provider, model, project=project,
                              tokens_in=int(ti) if ti else None,
                              tokens_out=int(to) if to else None,
                              reasoning_tokens=int(rt) if rt else None,
                              chars_in=int(ci) if ci else None,
                              chars_out=int(co) if co else None,
                              chars_reasoning=int(cr) if cr else None,
                              session_id=sid,
                              message_id=mid)
            self.send_json({'success': bool(result.get('ok')), **result})
            return

        # ----- OpenAI-compatible chat completions proxy -----
        # Added 2026-06-04 per lunar todo 8b635506: selena-project-lunarisis needs
        # an internal LLM endpoint so the orchestrator / reflection pipeline
        # can call MiniMax-M3 (or any openclaw/* agent) without each subsystem
        # having to know the OpenClaw gateway password. This proxy:
        #   - authenticates the caller with selena-project's own auth
        #   - forwards the body to http://localhost:18789/v1/chat/completions
        #     with the gateway's password from ~/.openclaw/openclaw.json
        #   - records the call in llm_call_tracker so budget tracking sees it
        #   - returns the gateway's response as-is (OpenAI chat-completions shape)
        if path == '/v1/chat/completions':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                raw = b''
                if content_length:
                    raw = self.rfile.read(content_length)
                body = json.loads(raw.decode('utf-8')) if raw else {}
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json({'error': f'invalid JSON body: {e}'}, 400)
                return
            if not isinstance(body, dict):
                self.send_json({'error': 'body must be a JSON object'}, 400)
                return
            if not body.get('messages'):
                self.send_json({'error': 'messages is required'}, 400)
                return
            # Default model → openclaw (gateway routes to minimax-portal/M3).
            if 'model' not in body or not body['model']:
                body['model'] = 'openclaw'
            # Read gateway password (cached after first read).
            gw_url, gw_pw = _get_openclaw_gateway()
            if not gw_pw:
                self.send_json({'error': 'OpenClaw gateway password not found in ~/.openclaw/openclaw.json'}, 503)
                return
            import urllib.request
            import urllib.error
            payload = json.dumps(body).encode('utf-8')
            req = urllib.request.Request(
                f"{gw_url}/v1/chat/completions",
                data=payload,
                method='POST',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {gw_pw}',
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp_body = resp.read()
                    resp_status = resp.status
            except urllib.error.HTTPError as e:
                # Forward the gateway's error body so the caller sees the real reason.
                err_body = e.read().decode('utf-8', errors='replace')[:1000]
                try:
                    self.send_json(json.loads(err_body), e.code)
                except Exception:
                    self.send_json({'error': f'gateway HTTP {e.code}', 'detail': err_body}, e.code)
                return
            except Exception as e:
                self.send_json({'error': f'gateway request failed: {e}'}, 502)
                return
            # Parse the gateway response so we can pull the real usage field
            # (tokens) and the assistant text (for char counts).  This proxy
            # is the choke point for every lunar-side LLM call (reflection
            # pipeline, coding worker, etc.), so capturing here is what
            # makes per-project spend visible to the cost tracker.
            try:
                parsed = json.loads(resp_body.decode('utf-8'))
            except Exception:
                parsed = {'raw': resp_body.decode('utf-8', errors='replace')[:2000]}
            # ---- Best-effort budget tracking with real usage + char counts
            # Don't fail the call if the tracker errors; the response is
            # the user-facing thing and we already paid the token cost.
            try:
                usage = (parsed.get('usage') or {}) if isinstance(parsed, dict) else {}
                tokens_in = usage.get('prompt_tokens')
                tokens_out = usage.get('completion_tokens')
                ctd = usage.get('completion_tokens_details') or {}
                reasoning_tokens = ctd.get('reasoning_tokens')
                # Char counts: sum of message contents in + assistant content
                # out + reasoning out (if present). Cheap (pure-python),
                # zero-cost, runs only on the proxy path.
                def _count_msg_chars(msgs):
                    n = 0
                    for m in (msgs or []):
                        c = m.get('content') if isinstance(m, dict) else None
                        if isinstance(c, str):
                            n += len(c)
                        elif isinstance(c, list):
                            for part in c:
                                if isinstance(part, dict):
                                    n += len(part.get('text') or '')
                    return n
                chars_in = _count_msg_chars(body.get('messages'))
                choice0 = ((parsed.get('choices') or [{}])[0]
                           if isinstance(parsed, dict) else {})
                msg0 = choice0.get('message') or {}
                assistant_text = msg0.get('content') or ''
                if not isinstance(assistant_text, str):
                    assistant_text = ''
                reasoning_text = msg0.get('reasoning_content') or ''
                if not isinstance(reasoning_text, str):
                    reasoning_text = ''
                chars_out = len(assistant_text)
                chars_reasoning = len(reasoning_text)
                tracker = _get_llm_tracker()
                # Pull session/message ids from request headers (if
                # the caller is the OpenClaw gateway) or from the body
                # (if the caller embeds them in metadata). Either way
                # this gives us a dedup key so the reconciler won't
                # re-add the same call. Added 2026-06-08 to stop the
                # chat-proxy record() from silently double-counting.
                sid = (self.headers.get('X-OpenClaw-Session-Id')
                       or body.get('session_id')
                       or body.get('metadata', {}).get('session_id') if isinstance(body.get('metadata'), dict) else None)
                mid = (self.headers.get('X-OpenClaw-Message-Id')
                       or body.get('message_id')
                       or body.get('metadata', {}).get('message_id') if isinstance(body.get('metadata'), dict) else None)
                record_result = tracker.record(
                    'minimax-portal',
                    body.get('model', 'openclaw'),
                    project='project-lunaris',
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    reasoning_tokens=reasoning_tokens,
                    chars_in=chars_in,
                    chars_out=chars_out,
                    chars_reasoning=chars_reasoning,
                    session_id=sid,
                    message_id=mid,
                )
                log_api('GATEWAY_CHAT_RECORD', f'sid={sid} mid={mid} result={record_result.get("ok")} dup={record_result.get("duplicate", False)} inmem={record_result.get("inmem_only", False)}')
            except Exception:
                pass
            self.send_json(parsed, resp_status)
            log_api('GATEWAY_CHAT_COMPLETIONS', f'model={body.get("model")} status={resp_status}')
            return

        # Fall through to the unified _dispatch_routes table. Handles
        # routes shared across methods (notably the pending-action POST
        # routes added 2026-06-04) and returns its own 404 if nothing
        # matches. This chaining fixes the bug where POST routes had
        # been pasted into the do_GET body and never fired for non-GET
        # requests, causing the user to see "Clear failed: Unexpected
        # token '<'" (server returned 501 + HTML).
        self._dispatch_routes(path, 'POST')

    def do_PUT(self):
        """Handle PUT requests - supports /api/todos/update"""
        parsed = urlparse(self.path)
        path = parsed.path
        path = parsed.path
        
        # Handle CORS preflight
        if path == '/api/todos/update':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            query = parse_qs(parsed.query)
            todo_id = query.get('id', [''])[0]
            if not todo_id:
                self.send_json({'success': False, 'error': 'id required'}, 400)
                return
            # Extract update fields
            updates = {}
            if 'short_desc' in query: updates['short_desc'] = query['short_desc'][0]
            if 'long_desc' in query: updates['long_desc'] = query['long_desc'][0]
            if 'priority' in query: updates['priority'] = int(query['priority'][0])
            if 'status' in query: updates['status'] = query['status'][0]
            if 'deleted_at' in query: updates['deleted_at'] = query['deleted_at'][0] if query['deleted_at'][0] else None
            if 'sensitive' in query: updates['sensitive'] = query['sensitive'][0].lower() == 'true'
            if 'parent_id' in query: updates['parent_id'] = query['parent_id'][0] if query['parent_id'][0] else None
            if 'estimated_llm_calls' in query: updates['estimated_llm_calls'] = int(query['estimated_llm_calls'][0]) if query['estimated_llm_calls'][0] else None
            if 'creator_id' in query: updates['creator_id'] = query['creator_id'][0] if query['creator_id'][0] else None
            if 'conversation_id' in query: updates['conversation_id'] = query['conversation_id'][0] if query['conversation_id'][0] else None
            if 'agent_id' in query: updates['agent_id'] = query['agent_id'][0] if query['agent_id'][0] else None
            if 'project' in query: updates['project'] = query['project'][0] if query['project'][0] else None
            if 'agent_owner' in query: updates['agent_owner'] = query['agent_owner'][0] if query['agent_owner'][0] else None
            if 'what_happened' in query: updates['what_happened'] = query['what_happened'][0] if query['what_happened'][0] else None
            if 'irreversible' in query: updates['irreversible'] = query['irreversible'][0].lower() == 'true'
            if 'block_reason' in query: updates['block_reason'] = query['block_reason'][0] if query['block_reason'][0] else None
            if 'waiting_for' in query: updates['waiting_for'] = query['waiting_for'][0] if query['waiting_for'][0] else None
            # completed_at: explicit value wins over the auto-rule. Empty string
            # is treated as "not set" so the auto-rule (set/clear on status
            # transitions) handles it. See TodoManager._apply_completed_at_rule.
            if 'completed_at=' in parsed.query:
                _ca = query.get('completed_at', [''])[0]
                updates['completed_at'] = _ca if _ca else None
            if 'restore' in query: updates['restore'] = query['restore'][0].lower() == 'true'
            todo = todo_manager.update_todo(todo_id, **updates)
            if todo:
                self.send_json({'success': True, 'todo': todo})
            else:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
            return
        
        # 404 for unsupported PUT endpoints
        self._dispatch_routes(path, 'PUT')

    def do_DELETE(self):
        """Handle DELETE requests. As of 2026-06-04 the only DELETE routes
        are the pending-action delete/clear endpoints. Falls through to 404
        for anything else.
        """
        parsed = urlparse(self.path)
        path = parsed.path

        # ── CORS preflight ──
        if self.command == 'OPTIONS':
            self._send_cors_preflight()
            return

        # ── Pending action delete (normal lane) ──
        # Path: /api/discord-lookup/pending/<id>
        if path.startswith('/api/discord-lookup/pending/') and self.command == 'DELETE':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            pending_id = path[len('/api/discord-lookup/pending/'):]
            if not pending_id:
                self.send_json({'error': 'id required in path'}, 400)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'pending', 'delete', '--id', pending_id],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # ── Urgent action clear (urgent lane) ──
        # Path: /api/discord-lookup/pending-urgent/<id>
        if path.startswith('/api/discord-lookup/pending-urgent/') and self.command == 'DELETE':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            urgent_id = path[len('/api/discord-lookup/pending-urgent/'):]
            if not urgent_id:
                self.send_json({'error': 'id required in path'}, 400)
                return
            try:
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'pending', 'urgent-clear', '--id', urgent_id],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                if out.returncode != 0:
                    self.send_json({'error': f'CLI exit {out.returncode}', 'stderr': out.stderr[-500:]}, 500)
                    return
                self.send_json(json.loads(out.stdout))
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            return

        # 404 for unsupported DELETE endpoints
        self._dispatch_routes(path, 'DELETE')

    def do_PATCH(self):
        """Handle PATCH requests — Discord Lookup settings update."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/discord-lookup/settings':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                body = json.loads(raw) if raw else {}
            except (ValueError, json.JSONDecodeError) as e:
                self.send_json({'error': f'invalid JSON body: {e}'}, 400)
                return
            if not isinstance(body, dict) or not body:
                self.send_json({'error': 'body must be a non-empty JSON object of {key: value}'}, 400)
                return
            results = []
            for k, v in body.items():
                if isinstance(v, (list, dict)):
                    val_str = json.dumps(v)
                elif isinstance(v, bool):
                    val_str = 'true' if v else 'false'
                else:
                    val_str = str(v)
                out = subprocess.run(
                    ['python3', str(Path(SELENA_ROOT) / 'scripts' / 'discord_lookup.py'),
                     'settings', 'set', '--key', k, '--value', val_str],
                    capture_output=True, text=True, timeout=15, cwd=str(SELENA_ROOT)
                )
                results.append({'key': k, 'ok': out.returncode == 0, 'stdout': out.stdout.strip()[:200],
                                'stderr': out.stderr.strip()[:200] if out.returncode != 0 else None})
            log_api('DISCORD_LOOKUP_SETTINGS', f'updated {len(body)} key(s) via PATCH')
            self.send_json({'success': all(r['ok'] for r in results), 'results': results})
            return

        # 404 for unsupported PATCH endpoints
        self._dispatch_routes(path, 'PATCH')


def main():
    """Main entry point"""
    print(f"🤖 Selena v2 API Server")
    print(f"   Port: {PORT}")
    print(f"   Root: {SELENA_ROOT}")
    print(f"   Password: {'Configured' if WEB_PASSWORD != 'change_me' else 'NOT SET!'}")
    print()
    print(f"   Login: GET /api/login?password=<password>")
    print(f"   Status: GET /api/status (Bearer token required)")
    print(f"   Files: GET /api/files (Bearer token required)")
    print(f"   Memory: GET /api/memory (Bearer token required)")
    print(f"   Agents: GET /api/agents (Bearer token required)")
    print()
    print(f"   Web: http://localhost:{PORT}/")
    print()
    
    server = HTTPServer(('0.0.0.0', PORT), RequestHandler)
    print(f"🚀 Server running on http://0.0.0.0:{PORT}")
    
    # Auto-purge old deleted todos (older than 1 week)
    purged = todo_manager.purge_old_deleted(days=7)
    if purged > 0:
        print(f"🗑️ Purged {purged} old deleted todo(s)")
    
    # Auto-start self-evolution loop
    print("🧠 Auto-starting Self-Evolution Loop...")
    evolution_loop.start()

    # Auto-start service watchdog (auto-restart monitored services when offline)
    print("🛡️  Auto-starting Service Watchdog (30s poll, projects.md config)...")
    service_monitor.start()

    # Auto-start Discord notifier (direct integration, no LLM, no cron).
    # If disabled (DISCORD_ENABLED=false) or token missing, this is a no-op.
    print("💬 Auto-starting Discord Notifier (direct discord.py, no LLM/cron)...")
    n = get_default_notifier()
    n.start()  # safe to call even if not enabled; logs and returns False

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        service_monitor.stop()
        n.stop()
        server.shutdown()


if __name__ == '__main__':
    main()
