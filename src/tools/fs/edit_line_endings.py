"""Line-ending policy for literal edit anchors (fs_apply_file_edits, fs_search_and_replace).

Models write anchors with LF while files on Windows keep CRLF; the tools match
literally, so the anchor is resolved here: literal first, then converted to the
file's dominant style. The replacement follows the style of the anchor it
replaces (the file's style for single-line anchors), so an edit never introduces
a second line-ending style. File content itself is never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.file_text import convert_line_endings, detect_line_ending


@dataclass(frozen=True)
class EditLineEndings:
    search: str
    replace: str
    file_line_ending: str | None  # "\r\n" | "\n" | None when the file has no newlines
    normalized: bool  # search and/or replace were converted


def resolve_edit_line_endings(content: str, search: str, replace: str) -> EditLineEndings:
    file_eol = detect_line_ending(content)
    if file_eol is None:
        return EditLineEndings(search, replace, None, False)

    resolved_search = search
    if search not in content:
        candidate = convert_line_endings(search, file_eol)
        if candidate != search and candidate in content:
            resolved_search = candidate

    target_eol = detect_line_ending(resolved_search) or file_eol
    resolved_replace = convert_line_endings(replace, target_eol)
    normalized = resolved_search != search or resolved_replace != replace
    return EditLineEndings(resolved_search, resolved_replace, file_eol, normalized)
