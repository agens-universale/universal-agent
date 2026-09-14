#!/usr/bin/env python3
"""
Quick validation script for skills - minimal version
"""

import sys
import os
import re
import yaml
from pathlib import Path

def _check_name(frontmatter):
    name = frontmatter.get('name', '')
    if not isinstance(name, str):
        return f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    # Name is human-readable; only enforce length, not kebab-case (UTF/spaces allowed).
    if name and len(name) > 64:
        return f"Name is too long ({len(name)} characters). Maximum is 64 characters."
    return None


def _check_id(frontmatter):
    # id (если присутствует) — должен быть kebab-case
    skill_id = frontmatter.get('id', '')
    if not skill_id:
        return None
    if not isinstance(skill_id, str):
        return f"id must be a string, got {type(skill_id).__name__}"
    if not re.match(r'^[a-z0-9-]+$', skill_id):
        return f"id '{skill_id}' should be kebab-case (lowercase letters, digits, and hyphens only)"
    if skill_id.startswith('-') or skill_id.endswith('-') or '--' in skill_id:
        return f"id '{skill_id}' cannot start/end with hyphen or contain consecutive hyphens"
    return None


def _check_description(frontmatter):
    description = frontmatter.get('description', '')
    if not isinstance(description, str):
        return f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if not description:
        return None
    # Check for angle brackets
    if '<' in description or '>' in description:
        return "Description cannot contain angle brackets (< or >)"
    # Check description length (max 1024 characters per spec)
    if len(description) > 1024:
        return f"Description is too long ({len(description)} characters). Maximum is 1024 characters."
    return None


def _check_compatibility(frontmatter):
    # Validate compatibility field if present (optional)
    compatibility = frontmatter.get('compatibility', '')
    if not compatibility:
        return None
    if not isinstance(compatibility, str):
        return f"Compatibility must be a string, got {type(compatibility).__name__}"
    if len(compatibility) > 500:
        return f"Compatibility is too long ({len(compatibility)} characters). Maximum is 500 characters."
    return None


def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    # Check SKILL.md exists
    skill_md = skill_path / 'SKILL.md'
    if not skill_md.exists():
        return False, "SKILL.md not found"

    # Read and validate frontmatter
    content = skill_md.read_text(encoding='utf-8')
    if not content.startswith('---'):
        return False, "No YAML frontmatter found"

    # Extract frontmatter
    match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)

    # Parse YAML frontmatter
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    # Define allowed properties
    ALLOWED_PROPERTIES = {'id', 'name', 'description', 'version', 'license', 'allowed-tools', 'metadata', 'compatibility'}

    # Check for unexpected properties (excluding nested keys under metadata)
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        return False, (
            f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(unexpected_keys))}. "
            f"Allowed properties are: {', '.join(sorted(ALLOWED_PROPERTIES))}"
        )

    # Check required fields
    if 'name' not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if 'description' not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    for check in (_check_name, _check_id, _check_description, _check_compatibility):
        error = check(frontmatter)
        if error is not None:
            return False, error

    return True, "Skill is valid!"

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)
    
    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)