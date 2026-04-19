#!/usr/bin/env python3
"""
Workspace Scanner for Selena v2
===============================

Scans .md files in the workspace and extracts knowledge to add to the knowledge base.
"""

import os
import re
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
import json

# Configuration
SELENA_ROOT = os.path.expanduser("~/openclaw/workspace/selena-project")
WORKSPACE_ROOT = os.path.expanduser("~/openclaw/workspace")

# Directories to scan (relative to workspace root)
SCAN_DIRECTORIES = [
    ("selena-project", os.path.join(WORKSPACE_ROOT, "selena-project")),
    ("selena", os.path.join(WORKSPACE_ROOT, "selena")),
]

# Directories to skip
SKIP_PATTERNS = [
    "node_modules",
    ".git",
    ".brv",
    "__pycache__",
    "venv",
    ".venv",
    "data/backups",
    "old/selena/memory",  # Old memory files, skip
]

# File patterns to scan
SCAN_EXTENSIONS = [".md"]

# Maximum content length to process per file
MAX_CONTENT_LENGTH = 50000

@dataclass
class ScanEntry:
    """A knowledge entry extracted from a file."""
    category: str  # lesson, skill, pattern, reference
    title: str
    content: str
    tags: List[str]
    source_file: str

@dataclass
class ScanResult:
    """Result of a workspace scan."""
    timestamp: str
    files_scanned: int
    entries_added: int
    entries_updated: int
    errors: List[str]
    details: List[Dict]

class WorkspaceScanner:
    """Scans workspace .md files and extracts knowledge."""
    
    def __init__(self):
        self.knowledge_base = None  # Will be set later
        self.scan_history_file = os.path.join(SELENA_ROOT, "data", "scanner_history.json")
        self.last_result: Optional[ScanResult] = None
    
    def _init_knowledge_base(self):
        """Lazy load knowledge base to avoid circular imports."""
        if self.knowledge_base is None:
            from knowledge_base import knowledge_base
            self.knowledge_base = knowledge_base
    
    def _should_skip(self, filepath: str) -> bool:
        """Check if a file should be skipped."""
        filepath_normalized = filepath.replace("\\", "/")
        for pattern in SKIP_PATTERNS:
            if pattern in filepath_normalized:
                return True
        return False
    
    def _extract_title_from_content(self, content: str, filename: str) -> str:
        """Extract a title from the content or filename."""
        # Try to find a markdown title (# Title)
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            title = match.group(1).strip()
            # Truncate if too long
            if len(title) > 80:
                title = title[:77] + "..."
            return title
        
        # Try to find the first non-empty line that's not a header
        lines = content.split("\n")
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                if len(line) > 80:
                    line = line[:77] + "..."
                return line
        
        # Fallback to filename without extension
        return os.path.splitext(os.path.basename(filename))[0]
    
    def _categorize_content(self, content: str, filename: str) -> str:
        """Determine the best category for the content."""
        content_lower = content.lower()
        filename_lower = filename.lower()
        
        # Check filename for hints
        if "lesson" in filename_lower or "learn" in filename_lower:
            return "lesson"
        if "skill" in filename_lower or "how-to" in filename_lower:
            return "skill"
        if "pattern" in filename_lower or "architecture" in filename_lower:
            return "pattern"
        if "reference" in filename_lower or "api" in filename_lower:
            return "reference"
        
        # Check content for hints
        if any(word in content_lower for word in ["lesson learned", "learned:", "mistake:", "error:"]):
            return "lesson"
        if any(word in content_lower for word in ["how to", "tutorial", "step by step", "workflow"]):
            return "skill"
        if any(word in content_lower for word in ["pattern:", "architecture:", "design:", "structure:"]):
            return "pattern"
        if any(word in content_lower for word in ["api:", "reference:", "documentation:", "docs:"]):
            return "reference"
        
        # Check if it's a memory or reflection file
        if "memory" in filename_lower or "reflection" in filename_lower:
            return "lesson"
        
        # Default to reference for documentation-like content
        if any(word in content_lower for word in ["# ", "## ", "### "]) and len(content) > 500:
            return "reference"
        
        return "reference"
    
    def _extract_tags(self, content: str, filename: str, category: str) -> List[str]:
        """Extract relevant tags from content."""
        tags = []
        
        # Add category as a tag
        tags.append(category)
        
        # Add source project
        if "selena-project" in filename:
            tags.append("selena-project")
        elif "selena" in filename:
            tags.append("selena")
        
        # Extract hashtags from content
        hashtags = re.findall(r"#(\w+)", content)
        for tag in hashtags:
            if tag.lower() not in ["md", "html", "css", "js", "py", "api"]:
                tags.append(tag.lower())
        
        # Limit to 10 tags
        return list(set(tags))[:10]
    
    def _clean_content(self, content: str) -> str:
        """Clean and truncate content for knowledge base."""
        # Remove markdown headers from the start (we'll use title instead)
        lines = content.split("\n")
        cleaned_lines = []
        found_content = False
        
        for line in lines:
            # Skip the first title header
            if not found_content and line.strip().startswith("#"):
                found_content = True
                continue
            cleaned_lines.append(line)
        
        content = "\n".join(cleaned_lines).strip()
        
        # Truncate if too long
        if len(content) > MAX_CONTENT_LENGTH:
            content = content[:MAX_CONTENT_LENGTH] + "\n\n[Content truncated...]"
        
        return content
    
    def _is_duplicate(self, title: str, content: str, category: str) -> Optional[dict]:
        """Check if a similar entry already exists in the knowledge base."""
        self._init_knowledge_base()
        
        # Get all entries in the category
        entries = self.knowledge_base.get_entries(category=category)
        
        # Create hash of the content
        content_hash = hashlib.md5(content[:1000].encode()).hexdigest()
        
        for entry in entries:
            # Check if title is similar
            title_similarity = self._similar_strings(title.lower(), entry.get("title", "").lower())
            if title_similarity > 0.8:
                return entry
            
            # Check if content is similar
            entry_content = entry.get("content", "")[:1000]
            content_similarity = self._similar_strings(content[:1000].lower(), entry_content.lower())
            if content_similarity > 0.9:
                return entry
        
        return None
    
    def _similar_strings(self, s1: str, s2: str) -> float:
        """Calculate similarity between two strings (simple Jaccard-like)."""
        words1 = set(s1.split())
        words2 = set(s2.split())
        if not words1 or not words2:
            return 0.0
        intersection = words1 & words2
        union = words1 | words2
        return len(intersection) / len(union)
    
    def _extract_entries_from_file(self, filepath: str) -> List[ScanEntry]:
        """Extract knowledge entries from a single file."""
        entries = []
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return [ScanEntry(
                category="lesson",
                title=f"Error reading {os.path.basename(filepath)}",
                content=f"Could not read file: {str(e)}",
                tags=["error", "file-read-error"],
                source_file=filepath
            )]
        
        # Skip very short files (probably not useful)
        if len(content) < 100:
            return []
        
        # Extract metadata
        title = self._extract_title_from_content(content, filepath)
        category = self._categorize_content(content, filepath)
        tags = self._extract_tags(content, filepath, category)
        cleaned_content = self._clean_content(content)
        
        entry = ScanEntry(
            category=category,
            title=title,
            content=cleaned_content,
            tags=tags,
            source_file=filepath
        )
        
        entries.append(entry)
        
        # Also look for key sections that could be separate entries
        # Split by ## headers and create separate entries for major sections
        sections = re.split(r"\n##\s+(.+)\n", content)
        if len(sections) > 1:
            for i in range(1, len(sections), 2):
                if i < len(sections):
                    section_title = sections[i]
                    section_content = sections[i + 1] if i + 1 < len(sections) else ""
                    
                    # Skip very short sections
                    if len(section_content.strip()) < 200:
                        continue
                    
                    # Create a sub-entry for this section
                    full_title = f"{title}: {section_title}"
                    if len(full_title) > 100:
                        full_title = full_title[:97] + "..."
                    
                    sub_entry = ScanEntry(
                        category=category,
                        title=full_title,
                        content=section_content.strip()[:10000],  # Limit section content
                        tags=tags + ["section"],
                        source_file=filepath
                    )
                    entries.append(sub_entry)
        
        return entries
    
    def scan_workspace(self) -> ScanResult:
        """Scan all .md files in the workspace and add to knowledge base."""
        self._init_knowledge_base()
        
        result = ScanResult(
            timestamp=datetime.now().isoformat(),
            files_scanned=0,
            entries_added=0,
            entries_updated=0,
            errors=[],
            details=[]
        )
        
        all_entries: List[ScanEntry] = []
        
        # Scan each directory
        for dir_name, dir_path in SCAN_DIRECTORIES:
            if not os.path.exists(dir_path):
                result.errors.append(f"Directory not found: {dir_path}")
                continue
            
            for root, dirs, files in os.walk(dir_path):
                # Filter out skip directories
                dirs[:] = [d for d in dirs if not self._should_skip(os.path.join(root, d))]
                
                for filename in files:
                    if not any(filename.endswith(ext) for ext in SCAN_EXTENSIONS):
                        continue
                    
                    filepath = os.path.join(root, filename)
                    
                    if self._should_skip(filepath):
                        continue
                    
                    result.files_scanned += 1
                    
                    try:
                        entries = self._extract_entries_from_file(filepath)
                        all_entries.extend(entries)
                    except Exception as e:
                        result.errors.append(f"Error scanning {filepath}: {str(e)}")
        
        # Add entries to knowledge base
        for entry in all_entries:
            try:
                # Check for duplicates
                existing = self._is_duplicate(entry.title, entry.content, entry.category)
                
                if existing:
                    result.entries_updated += 1
                    detail_action = "updated"
                else:
                    # Add to knowledge base
                    self.knowledge_base.add_entry(
                        category=entry.category,
                        title=entry.title,
                        content=entry.content,
                        tags=entry.tags
                    )
                    result.entries_added += 1
                    detail_action = "added"
                
                result.details.append({
                    "file": os.path.relpath(entry.source_file, WORKSPACE_ROOT),
                    "title": entry.title,
                    "category": entry.category,
                    "action": detail_action
                })
            except Exception as e:
                result.errors.append(f"Error adding entry '{entry.title}': {str(e)}")
        
        # Save last result
        self.last_result = result
        self._save_scan_history(result)
        
        return result
    
    def _save_scan_history(self, result: ScanResult):
        """Save scan result to history file."""
        os.makedirs(os.path.dirname(self.scan_history_file), exist_ok=True)
        
        # Load existing history
        history = []
        if os.path.exists(self.scan_history_file):
            try:
                with open(self.scan_history_file, 'r') as f:
                    history = json.load(f)
            except:
                history = []
        
        # Add new result
        history.append(asdict(result))
        
        # Keep only last 10 results
        history = history[-10:]
        
        with open(self.scan_history_file, 'w') as f:
            json.dump(history, f, indent=2)
    
    def get_last_result(self) -> Optional[ScanResult]:
        """Get the last scan result."""
        if self.last_result:
            return self.last_result
        
        # Try to load from file
        if os.path.exists(self.scan_history_file):
            try:
                with open(self.scan_history_file, 'r') as f:
                    history = json.load(f)
                if history:
                    last = history[-1]
                    return ScanResult(**last)
            except:
                pass
        
        return None
    
    def get_scan_history(self) -> List[ScanResult]:
        """Get scan history."""
        if os.path.exists(self.scan_history_file):
            try:
                with open(self.scan_history_file, 'r') as f:
                    history = json.load(f)
                return [ScanResult(**h) for h in history]
            except:
                pass
        return []


# Global instance
scanner = WorkspaceScanner()


# Convenience functions
def scan_workspace() -> ScanResult:
    """Scan the workspace and add knowledge."""
    return scanner.scan_workspace()

def get_last_scan() -> Optional[ScanResult]:
    """Get the last scan result."""
    return scanner.get_last_result()

def get_scan_history() -> List[ScanResult]:
    """Get scan history."""
    return scanner.get_scan_history()
