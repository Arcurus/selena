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

# Configuration
PORT = int(os.getenv('SELENA_PORT', '8765'))
WEB_PASSWORD = os.getenv('WEB_PASSWORD', 'change_me')
API_PASSWORD = os.getenv('WEB_PASSWORD', 'change_me')
SELENA_ROOT = os.path.expanduser('~/openclaw/workspace/selena')

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
        """Check if request is authenticated"""
        auth_header = self.headers.get('Authorization', '')
        
        if not auth_header.startswith('Bearer '):
            return False
        
        token = auth_header[7:]
        return active_tokens.get(token, False)
    
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
                self.send_json({'success': True, 'token': token})
            else:
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
        
        # Todo Manager endpoints
        if path == '/api/todos':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            # Get all todos
            status = parsed.query.split('status=')[1].split('&')[0] if 'status=' in parsed.query else None
            sort_by = parsed.query.split('sort_by=')[1].split('&')[0] if 'sort_by=' in parsed.query else 'priority'
            todos = todo_manager.get_all_todos(status=status, sort_by=sort_by)
            summary = todo_manager.get_summary()
            self.send_json({'todos': todos, 'summary': summary})
            return
        
        if path == '/api/todos/summary':
            if not self.authenticate():
                self.send_json({'error': 'Unauthorized'}, 401)
                return
            self.send_json(todo_manager.get_summary())
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
            if not short_desc:
                self.send_json({'success': False, 'error': 'short_desc required'}, 400)
                return
            todo = todo_manager.add_todo(short_desc, long_desc, priority)
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
            todo = todo_manager.update_todo(todo_id, **updates)
            if todo:
                self.send_json({'success': True, 'todo': todo})
            else:
                self.send_json({'success': False, 'error': 'Todo not found'}, 404)
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
            file_path = os.path.join(SELENA_ROOT, 'web', path[8:])
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
