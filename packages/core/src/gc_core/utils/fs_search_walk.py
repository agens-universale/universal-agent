"""Hard walk policy for FS *search* tools (name locate / content find).

Listing (browse) tools intentionally show the real tree. Search tools always
apply a non-removable floor of nested-directory skips so deployment config
cannot re-open junk trees by clearing ``exclude_dir_names``.

Config may only *add* basename excludes on top of the floor.
The start path of a walk is never skipped for these reasons (caller may set
``path`` to a junk dir to search inside it).
"""

from __future__ import annotations

from typing import AbstractSet, Iterable, Optional

# Nested dir basenames never descended into (start path itself is always kept).
# Not configurable — removing names from YAML must not re-enable these trees.
HARD_EXCLUDE_DIR_NAMES: frozenset[str] = frozenset({"venv", "__pycache__", "node_modules", ".git"})

# Nested directories whose basename starts with '.' are never descended into.
# Not configurable (start path still kept).
HARD_EXCLUDE_NESTED_DOT_DIRS: bool = True


def effective_exclude_dir_names(
    extra: Optional[Iterable[str]] = None,
) -> frozenset[str]:
    """Hard floor ∪ optional extra basenames from deployment config."""
    names: set[str] = set(HARD_EXCLUDE_DIR_NAMES)
    if extra:
        for item in extra:
            if isinstance(item, str) and item:
                names.add(item)
    return frozenset(names)


def should_skip_nested_dir_name(
    name: str,
    *,
    exclude_names: AbstractSet[str],
    exclude_dot_dirs: bool = HARD_EXCLUDE_NESTED_DOT_DIRS,
) -> Optional[str]:
    """If *name* (basename) must not be descended into, return a short reason key.

    Reasons: ``excluded_directory_name`` | ``hidden_dot_directory``.
    Does not apply agent-hidden (``.gc``) — callers handle that separately.
    Does not know start-path identity — caller must keep the walk root.
    """
    if name in exclude_names:
        return "excluded_directory_name"
    if exclude_dot_dirs and name.startswith("."):
        return "hidden_dot_directory"
    return None


def hard_exclude_dir_names_for_prompt() -> str:
    """Stable, human-readable list for tool descriptions (matches the floor)."""
    return ", ".join(sorted(HARD_EXCLUDE_DIR_NAMES))
