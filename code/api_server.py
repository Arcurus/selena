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
from todo_manager import todo_manager
from knowledge_base import knowledge_base as kb
from workspace_scanner import scanner, scan_workspace, get_last_scan, get_scan_history

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
    
    def do_OPTIONS(self):
        """Handle CORS preflight"""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests"""
        parsed = urlparse(self.path)
        path = parsed.path
        
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
        
        if path == '/api/llm-calls/increment':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            count = track_llm_call()
            self.send_json({'success': True, 'count': count})
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
                    entries.append({
                        'name': name,
                        'path': rel_path,
                        'is_dir': is_dir,
                        'size': 0 if is_dir else os.path.getsize(entry_path)
                    })
                self.send_json({'success': True, 'path': dir_path, 'entries': entries})
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
        
        # Todo Manager endpoints
        if path == '/api/todos':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Get all todos
            query = parse_qs(parsed.query)
            status = query.get('status', [None])[0]
            sort_by = query.get('sort_by', ['priority'])[0]
            sensitive = None
            if 'sensitive' in query:
                sensitive = query['sensitive'][0].lower() == 'true'
            todos = todo_manager.get_all_todos(status=status, sort_by=sort_by, sensitive=sensitive)
            summary = todo_manager.get_summary(sensitive=sensitive)
            self.send_json({'todos': todos, 'summary': summary})
            return
        
        if path == '/api/todos/summary':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            sensitive = None
            if 'sensitive' in parsed.query:
                sensitive = parsed.query.split('sensitive=')[1].split('&')[0].lower() == 'true'
            self.send_json(todo_manager.get_summary(sensitive=sensitive))
            return
        
        if path == '/api/todos/add':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Parse query params
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
            if not short_desc:
                self.send_json({'success': False, 'error': 'short_desc required'}, 400)
                return
            todo = todo_manager.add_todo(short_desc, long_desc, priority, sensitive, parent_id, estimated_llm_calls, creator_id, conversation_id, agent_id)
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
            if 'sensitive' in query: updates['sensitive'] = query['sensitive'][0].lower() == 'true'
            if 'parent_id' in query: updates['parent_id'] = query['parent_id'][0] if query['parent_id'][0] else None
            if 'estimated_llm_calls' in query: updates['estimated_llm_calls'] = int(query['estimated_llm_calls'][0]) if query['estimated_llm_calls'][0] else None
            if 'creator_id' in query: updates['creator_id'] = query['creator_id'][0] if query['creator_id'][0] else None
            if 'conversation_id' in query: updates['conversation_id'] = query['conversation_id'][0] if query['conversation_id'][0] else None
            if 'agent_id' in query: updates['agent_id'] = query['agent_id'][0] if query['agent_id'][0] else None
            if 'block_reason' in query: updates['block_reason'] = query['block_reason'][0] if query['block_reason'][0] else None
            if 'waiting_for' in query: updates['waiting_for'] = query['waiting_for'][0] if query['waiting_for'][0] else None
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
            todo = todo_manager.mark_done(todo_id)
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
        
        if path == '/api/services/restart' or path == '/api/services/stop':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            
            # Parse query string manually since we need service name
            parsed = urlparse(path)
            query = parse_qs(parsed.query) if '?' in path else {}
            
            # Alternative: parse from full path
            if 'service' not in query:
                from urllib.parse import parse_qs as pqs
                query = pqs(parsed.query) if parsed.query else {}
            
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
                    pid_file = os.path.join(DATA_DIR, 'open_world.pid')
                    if os.path.exists(pid_file):
                        with open(pid_file, 'r') as f:
                            pid = int(f.read().strip())
                        try:
                            os.kill(pid, 9)
                            result['success'] = True
                            result['message'] = f'Stopped open-world-selena (PID {pid})'
                        except ProcessLookupError:
                            result['success'] = True
                            result['message'] = f'Process {pid} already gone'
                        except Exception as e:
                            result['error'] = str(e)
                    else:
                        result['error'] = 'No PID file found'
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
    
    # Auto-start self-evolution loop
    print("🧠 Auto-starting Self-Evolution Loop...")
    evolution_loop.start()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
        server.shutdown()


if __name__ == '__main__':
    main()
