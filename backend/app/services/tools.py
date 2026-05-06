import os
import subprocess
from typing import List, Optional
from langchain.tools import tool

def get_safe_path(base_path: str, target_path: str) -> str:
    """Ensures the target path is within the base path to prevent directory traversal."""
    absolute_base = os.path.abspath(base_path)
    absolute_target = os.path.abspath(os.path.join(base_path, target_path))
    if not absolute_target.startswith(absolute_base):
        return absolute_base
    return absolute_target

@tool
def list_files(repo_path: str, directory: str = ".") -> str:
    """
    Recursively lists all files in the given directory within the repository.
    repo_path: The absolute path to the repository root.
    directory: Relative path from repo root to list.
    """
    target_dir = get_safe_path(repo_path, directory)
    files_list = []
    
    for root, dirs, files in os.walk(target_dir):
        if any(ignored in root for ignored in ['.git', 'node_modules', '__pycache__', 'venv', '.next', 'dist', 'build']):
            continue
        
        rel_root = os.path.relpath(root, target_dir)
        level = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        
        if level > 3: # Limit depth to prevent overwhelming output
            continue
            
        indent = ' ' * 4 * level
        files_list.append(f"{indent}{os.path.basename(root)}/")
        sub_indent = ' ' * 4 * (level + 1)
        for f in files:
            files_list.append(f"{sub_indent}{f}")
            
    return "\n".join(files_list) if files_list else "Directory is empty or not found."

@tool
def read_file_content(repo_path: str, file_path: str) -> str:
    """
    Reads the content of a specific file. 
    repo_path: The absolute path to the repository root.
    file_path: Relative path from repo root to the file.
    """
    full_path = get_safe_path(repo_path, file_path)
    if not os.path.isfile(full_path):
        return f"Error: File {file_path} not found."
        
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
            if len(content) > 10000:
                return content[:10000] + "\n... (truncated for length)"
            return content
    except Exception as e:
        return f"Error reading file: {str(e)}"

@tool
def search_code_snippet(repo_path: str, query: str, directory: str = ".") -> str:
    """
    Searches for a specific string or pattern in the codebase using grep.
    repo_path: The absolute path to the repository root.
    query: The string to search for.
    directory: Relative path from repo root to search within.
    """
    target_dir = get_safe_path(repo_path, directory)
    try:
        result = subprocess.run(
            ['grep', '-r', '-l', query, target_dir],
            capture_output=True, text=True, timeout=10
        )
        files = result.stdout.splitlines()
        if not files:
            return f"No matches found for '{query}'."
        
        # Make paths relative to repo_path for the AI
        rel_files = [os.path.relpath(f, repo_path) for f in files[:15]]
        return f"Found matches in:\n" + "\n".join(rel_files)
    except subprocess.TimeoutExpired:
        return "Search timed out. Try a more specific query or directory."
    except Exception as e:
        return f"Error searching code: {str(e)}"

@tool
def web_search(query: str) -> str:
    """
    Searches the web for latest technical information or API documentation.
    Use this when you encounter unfamiliar libraries or need latest tech info.
    """
    # Placeholder for actual implementation (e.g., Tavily or Firecrawl)
    return f"Web search results for '{query}': (In production, this would return real-time snippets about the library or pattern requested.)"
