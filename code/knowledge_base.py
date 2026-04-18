#!/usr/bin/env python3
"""
Knowledge Base for Selena v2
=============================

A system to capture, store, and retrieve knowledge entries.
Entries are categorized as: lessons, skills, patterns, references.

Storage: Git-friendly JSON files in data/knowledge/
Each entry has: id, category, title, content, tags, created_at
"""

import os
import json
import uuid
from datetime import datetime
from typing import Optional, List

# Configuration
SELENA_ROOT = os.path.expanduser("~/openclaw/workspace/selena")
KNOWLEDGE_DIR = os.path.join(SELENA_ROOT, "data", "knowledge")
CATEGORIES = ["lesson", "skill", "pattern", "reference"]

class KnowledgeBase:
    """
    Manages knowledge entries for Selena v2.
    Entries are stored as JSON files, one per category.
    """
    
    def __init__(self):
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        self._ensure_category_files()
    
    def _ensure_category_files(self):
        """Ensure all category files exist."""
        for category in CATEGORIES:
            filepath = self._get_category_file(category)
            if not os.path.exists(filepath):
                self._save_category(category, [])
    
    def _get_category_file(self, category: str) -> str:
        """Get the filepath for a category."""
        return os.path.join(KNOWLEDGE_DIR, f"{category}s.json")
    
    def _load_category(self, category: str) -> List[dict]:
        """Load entries for a category."""
        filepath = self._get_category_file(category)
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _save_category(self, category: str, entries: List[dict]):
        """Save entries for a category."""
        filepath = self._get_category_file(category)
        with open(filepath, 'w') as f:
            json.dump(entries, f, indent=2)
    
    def _now(self) -> str:
        """Get current ISO timestamp."""
        return datetime.now().isoformat()
    
    def add_entry(self, category: str, title: str, content: str, 
                  tags: Optional[List[str]] = None) -> dict:
        """
        Add a new knowledge entry.
        
        Args:
            category: Type of entry (lesson, skill, pattern, reference)
            title: Brief title
            content: The knowledge content
            tags: Optional list of tags
        
        Returns:
            The created entry dict
        """
        if category not in CATEGORIES:
            raise ValueError(f"Invalid category. Must be one of: {CATEGORIES}")
        
        entry = {
            "id": str(uuid.uuid4())[:8],
            "category": category,
            "title": title,
            "content": content,
            "tags": tags or [],
            "created_at": self._now()
        }
        
        entries = self._load_category(category)
        entries.append(entry)
        self._save_category(category, entries)
        
        return entry
    
    def get_all_entries(self, category: Optional[str] = None,
                       search: Optional[str] = None) -> List[dict]:
        """
        Get knowledge entries, optionally filtered.
        
        Args:
            category: Filter by category
            search: Search in title and content
        
        Returns:
            List of matching entries
        """
        if category:
            categories = [category]
        else:
            categories = CATEGORIES
        
        results = []
        for cat in categories:
            entries = self._load_category(cat)
            results.extend(entries)
        
        # Search in title and content
        if search:
            search_lower = search.lower()
            results = [e for e in results if 
                      search_lower in e.get("title", "").lower() or
                      search_lower in e.get("content", "").lower()]
        
        # Sort by created_at (newest first)
        results.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        
        return results
    
    def get_entries(self, category: Optional[str] = None,
                   tag: Optional[str] = None,
                   search: Optional[str] = None) -> List[dict]:
        """Alias for get_all_entries for backwards compatibility."""
        return self.get_all_entries(category=category, search=search)
    
    def get_entry(self, entry_id: str) -> Optional[dict]:
        """Get a specific entry by ID."""
        for category in CATEGORIES:
            entries = self._load_category(category)
            for entry in entries:
                if entry.get("id") == entry_id:
                    return entry
        return None
    
    def update_entry(self, entry_id: str, 
                    title: Optional[str] = None,
                    content: Optional[str] = None,
                    tags: Optional[List[str]] = None,
                    category: Optional[str] = None) -> Optional[dict]:
        """Update an existing entry."""
        for cat in CATEGORIES:
            entries = self._load_category(cat)
            for i, entry in enumerate(entries):
                if entry.get("id") == entry_id:
                    if title is not None:
                        entry["title"] = title
                    if content is not None:
                        entry["content"] = content
                    if tags is not None:
                        entry["tags"] = tags
                    if category is not None and category in CATEGORIES:
                        # Move to different category if category changed
                        if category != cat:
                            entries.pop(i)
                            self._save_category(cat, entries)
                            entry["category"] = category
                            new_entries = self._load_category(category)
                            new_entries.append(entry)
                            self._save_category(category, new_entries)
                            return entry
                    entries[i] = entry
                    self._save_category(cat, entries)
                    return entry
        return None
    
    def delete_entry(self, entry_id: str) -> bool:
        """Delete an entry by ID."""
        for category in CATEGORIES:
            entries = self._load_category(category)
            new_entries = [e for e in entries if e.get("id") != entry_id]
            if len(new_entries) < len(entries):
                self._save_category(category, new_entries)
                return True
        return False
    
    def get_summary(self) -> dict:
        """Get a summary of all knowledge entries."""
        summary = {}
        for category in CATEGORIES:
            entries = self._load_category(category)
            summary[category] = len(entries)
        return summary
    
    def get_categories(self) -> List[dict]:
        """Get categories with entry counts."""
        return [
            {"name": cat, "count": len(self._load_category(cat))}
            for cat in CATEGORIES
        ]
    
    def get_all_tags(self) -> dict:
        """Get all tags organized by category."""
        tags = {cat: set() for cat in CATEGORIES}
        for category in CATEGORIES:
            entries = self._load_category(category)
            for entry in entries:
                for tag in entry.get("tags", []):
                    tags[category].add(tag)
        return {cat: sorted(list(tag_set)) for cat, tag_set in tags.items()}


# Global instance
knowledge_base = KnowledgeBase()


# Convenience functions for API use
def add_knowledge(category: str, title: str, content: str, 
                 tags: Optional[List[str]] = None) -> dict:
    """Add a knowledge entry."""
    return knowledge_base.add_entry(category, title, content, tags)

def get_knowledge(category: Optional[str] = None,
                 tag: Optional[str] = None,
                 search: Optional[str] = None) -> List[dict]:
    """Get knowledge entries."""
    return knowledge_base.get_entries(category, tag, search)

def get_all_knowledge(category: Optional[str] = None,
                     search: Optional[str] = None) -> List[dict]:
    """Get all knowledge entries."""
    return knowledge_base.get_all_entries(category, search)

def get_knowledge_summary() -> dict:
    """Get knowledge summary."""
    return knowledge_base.get_summary()

def get_knowledge_categories() -> List[dict]:
    """Get knowledge categories."""
    return knowledge_base.get_categories()
