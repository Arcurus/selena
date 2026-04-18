"""
Knowledge Base Module for Selena v2
Manages lessons learned, skills, patterns, and other knowledge.
"""

import json
import os
from datetime import datetime
from typing import Optional, List, Dict
import uuid

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
KNOWLEDGE_DIR = os.path.join(DATA_DIR, 'knowledge')

# Ensure knowledge directory exists
os.makedirs(KNOWLEDGE_DIR, exist_ok=True)

KNOWLEDGE_FILE = os.path.join(KNOWLEDGE_DIR, 'knowledge.json')

CATEGORIES = ['lessons', 'skills', 'patterns', 'references']


def _load_knowledge() -> List[Dict]:
    """Load knowledge entries from disk."""
    if os.path.exists(KNOWLEDGE_FILE):
        try:
            with open(KNOWLEDGE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_knowledge(knowledge: List[Dict]) -> bool:
    """Save knowledge entries to disk."""
    try:
        with open(KNOWLEDGE_FILE, 'w') as f:
            json.dump(knowledge, f, indent=2)
        return True
    except IOError:
        return False


def get_all_entries(category: Optional[str] = None, search: Optional[str] = None) -> List[Dict]:
    """Get all knowledge entries, optionally filtered."""
    knowledge = _load_knowledge()
    
    # Filter by category
    if category and category in CATEGORIES:
        knowledge = [k for k in knowledge if k.get('category') == category]
    
    # Filter by search term
    if search:
        search_lower = search.lower()
        knowledge = [k for k in knowledge if 
                     search_lower in k.get('title', '').lower() or
                     search_lower in k.get('content', '').lower() or
                     any(search_lower in tag.lower() for tag in k.get('tags', []))]
    
    # Sort by most recent first
    knowledge.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
    
    return knowledge


def get_entry(entry_id: str) -> Optional[Dict]:
    """Get a specific knowledge entry by ID."""
    knowledge = _load_knowledge()
    for entry in knowledge:
        if entry.get('id') == entry_id:
            return entry
    return None


def add_entry(category: str, title: str, content: str, tags: List[str] = None) -> Optional[Dict]:
    """Add a new knowledge entry."""
    if category not in CATEGORIES:
        return None
    
    now = datetime.now().isoformat()
    entry = {
        'id': str(uuid.uuid4())[:8],
        'category': category,
        'title': title[:200],  # Limit title length
        'content': content,
        'tags': tags or [],
        'created_at': now,
        'updated_at': now
    }
    
    knowledge = _load_knowledge()
    knowledge.append(entry)
    
    if _save_knowledge(knowledge):
        return entry
    return None


def update_entry(entry_id: str, title: Optional[str] = None, content: Optional[str] = None, 
                 tags: Optional[List[str]] = None, category: Optional[str] = None) -> Optional[Dict]:
    """Update an existing knowledge entry."""
    knowledge = _load_knowledge()
    
    for i, entry in enumerate(knowledge):
        if entry.get('id') == entry_id:
            if title is not None:
                entry['title'] = title[:200]
            if content is not None:
                entry['content'] = content
            if tags is not None:
                entry['tags'] = tags
            if category is not None and category in CATEGORIES:
                entry['category'] = category
            entry['updated_at'] = datetime.now().isoformat()
            
            if _save_knowledge(knowledge):
                return entry
            return None
    
    return None


def delete_entry(entry_id: str) -> bool:
    """Delete a knowledge entry."""
    knowledge = _load_knowledge()
    initial_length = len(knowledge)
    knowledge = [k for k in knowledge if k.get('id') != entry_id]
    
    if len(knowledge) < initial_length:
        return _save_knowledge(knowledge)
    return False


def get_categories() -> Dict[str, int]:
    """Get all categories with their entry counts."""
    knowledge = _load_knowledge()
    counts = {cat: 0 for cat in CATEGORIES}
    
    for entry in knowledge:
        cat = entry.get('category')
        if cat in counts:
            counts[cat] += 1
    
    return counts
