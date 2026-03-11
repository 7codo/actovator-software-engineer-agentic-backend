import json
import re
import yaml
from pathlib import Path
import importlib.resources
from typing import Any

def read_file_from_init(filename: str, package: str) -> str:
    with importlib.resources.files(package).joinpath(filename).open("r", encoding="utf-8") as f:
        return f.read()
    



def parse_list_dir_tool_result(result: Any) -> tuple[list[str], list[str]]:
    """Safely parse a JSON string or dict into (dirs, files)."""
    # If it's a string, try to parse it as JSON first
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return [], []

    if not isinstance(result, dict):
        return [], []

    dirs = result.get("dirs", []) or []
    files = result.get("files", []) or []

    if not isinstance(dirs, list):
        dirs = []
    if not isinstance(files, list):
        files = []

    dirs = [d for d in dirs if isinstance(d, str)]
    files = [f for f in files if isinstance(f, str)]

    return dirs, files


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """
    Safely parse YAML frontmatter from a markdown string.

    Returns a tuple of (metadata dict, body content).
    If no frontmatter is found, returns ({}, original content).
    """
    pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    match = pattern.match(content)

    if not match:
        return {}, content

    raw_yaml = match.group(1)
    body = content[match.end():]

    try:
        metadata = yaml.safe_load(raw_yaml) or {}
        if not isinstance(metadata, dict):
            return {}, content
    except yaml.YAMLError:
        return {}, content

    return metadata, body


def parse_frontmatter_file(filepath: str) -> tuple[dict[str, Any], str]:
    """Read a markdown file and parse its frontmatter."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return parse_frontmatter(content)

# md = """\
# ---
# title: Hello World
# date: 2026-01-01
# tags: [python, markdown]
# ---

# # Hello World

# This is the body.
# ---
# Test: sss
# ---
# """

# metadata, body = parse_frontmatter(md)
# # metadata → {'title': 'Hello World', 'date': datetime.date(2026, 1, 1), 'tags': ['python', 'markdown']}
# # body     → "# Hello World\n\nThis is the body.\n"
# print("metadata", metadata)