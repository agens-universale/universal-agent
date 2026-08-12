"""Agent-owned scaffolding directories hidden from generic fs tool listings.

The agent creates a ``.gc/`` folder for its own config overlay, project skills
and notes. Generic fs tools (``fs_list_files``, ``fs_find_files_by_name``, ``fs_search_files``) hide it so
the agent neither sees nor edits its own internals. This is a domain rule about
agent scaffolding, so it lives in the tool layer -- not in the generic FsBackend.

Matching is by exact base name: ``.gc`` is hidden, but ``.gcignore`` / ``foo.gc``
and unrelated dotfiles (``.gitignore``) stay visible. This is deliberately NOT a
blanket dotfile filter.
"""

AGENT_HIDDEN_DIR_NAMES = frozenset({".gc"})


def is_agent_hidden(name: str) -> bool:
    """True if an entry base name is agent scaffolding to hide from fs listings."""
    return name in AGENT_HIDDEN_DIR_NAMES
