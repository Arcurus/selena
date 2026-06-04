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
import hashlib
import datetime
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
from scheduled_actions import scheduler
from priority_reflector import reflector, PriorityTask, PriorityReflector
from self_evolution import evolution_loop
from llm_call_tracker import get_tracker as _get_llm_tracker
from cost_tracker import build_daily_report as _ct_build_daily, build_weekly_report as _ct_build_weekly_report, render_markdown as _ct_render, post_to_discord as _ct_post
from todo_manager import todo_manager
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
        """Handle GET requests"""
        parsed = urlparse(self.path)
        path = parsed.path
        
        # Serve images from web/images/ directory
        if path.startswith('/images/'):
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
                try:
                    with open(full_path, 'rb') as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', len(content))
                    self.send_header('Cache-Control', 'public, max-age=3600')
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
        
        # Protected endpoints
        if path == '/api/logout':
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
            from scheduled_actions import load_scheduler_config
            cfg = load_scheduler_config()
            self.send_json({'success': True, 'config': cfg, 'status': scheduler.status()})
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
            self.send_json(t.status())
            return

        if path == '/api/llm-usage/sync':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
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
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            provider = qs.get('provider', [''])[0]
            model = qs.get('model', [''])[0]
            project = qs.get('project', [''])[0]
            ti = qs.get('tokens_in', [None])[0]
            to = qs.get('tokens_out', [None])[0]
            if not provider or not model:
                self.send_json({'error': 'provider and model required'}, 400)
                return
            t = _get_llm_tracker()
            t.record(provider, model, project=project,
                     tokens_in=int(ti) if ti else None,
                     tokens_out=int(to) if to else None)
            self.send_json({'success': True})
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
                todos = todo_manager.get_all_todos(status=status, sort_by=sort_by, sensitive=sensitive, include_deleted=include_deleted, search=search)
                summary = todo_manager.get_summary(sensitive=sensitive)
                log_api('LOAD_TODOS', f'status={status}, sort={sort_by}, sensitive={sensitive}, count={len(todos)}')
                self.send_json({'todos': todos, 'summary': summary})
            except Exception as e:
                log_error(f'/api/todos failed: {str(e)}', 'GET /api/todos')
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
                    except Exception as e:
                        log_error(f'POST /api/todos/add bad json: {e}', 'add')
            if not short_desc:
                self.send_json({'success': False, 'error': 'short_desc required'}, 400)
                return
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
            todo = todo_manager.add_todo(short_desc, long_desc, priority, sensitive, parent_id, estimated_llm_calls, creator_id, conversation_id, agent_id, project, agent_owner, what_happened)
            self.send_json({'success': True, 'todo': todo})
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
            # No auth required - projects are public info
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
                except Exception as e:
                    pass
            
            # Add default projects if none found
            if not projects:
                projects = [
                    {'name': 'open-world-selena', 'description': 'Rust-based evolving world with LLM-driven entities', 'status': 'active', 'port': '8081', 'repo': 'https://github.com/Arcurus/openworld-selena'},
                    {'name': 'selena-project', 'description': 'Self-development, memory, reflection system', 'status': 'active', 'port': '8765', 'repo': 'https://github.com/Arcurus/selena'}
                ]
            
            self.send_json({'projects': projects})
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
        if path == '/api/discord/health':
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
                        import subprocess
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
                    self.send_html(f.read())
            else:
                self.send_html('<html><body><h1>Web interface not found</h1></body></html>', 404)
            return
        
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
                elif file_path.endswith('.png'):
                    content_type = 'image/png'
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
        
        # Handle CORS preflight
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

            todo = todo_manager.add_todo(short_desc, long_desc, priority, sensitive, parent_id, estimated_llm_calls, creator_id, conversation_id, agent_id, project, agent_owner, what_happened)
            self.send_json({'success': True, 'todo': todo})
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

        # 404 for unsupported POST endpoints
        self.send_json({'error': 'Not found'}, 404)

    def do_PUT(self):
        """Handle PUT requests - supports /api/todos/update"""
        parsed = urlparse(self.path)
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
        self.send_json({'error': 'Not found'}, 404)


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
