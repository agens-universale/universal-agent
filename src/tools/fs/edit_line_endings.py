"""Line-ending policy for literal edit anchors (fs_apply_file_edits, fs_search_and_replace).

Models write anchors with LF while files on Windows keep CRLF; the tools match
literally, so the anchor is resolved here: literal first, then converted to the
file's style (both styles when the file mixes them). The replacement follows the
line ending of the matched region — a multi-line anchor carries its own, a
single-line anchor takes the line it sits on — so an edit never introduces a
second style into that region. File content itself is never rewritten.

``mode="preserve"`` switches all of this off: search and replace are used byte
for byte (deliberate line-ending changes, protocol fixtures).
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.file_text import (
    convert_line_endings,
    detect_line_ending,
    has_mixed_line_endings,
    line_ending_at,
)

NEWLINE_MODES = ("auto", "preserve")


@dataclass(frozen=True)
class EditLineEndings:
    search: str
    replace: str
    file_line_ending: str | None  # "\r\n" | "\n" | None when the file has no newlines
    normalized: bool  # search and/or replace were converted


def resolve_edit_line_endings(
    content: str, search: str, replace: str, *, mode: str = "auto"
) -> EditLineEndings:
    file_eol = detect_line_ending(content)
    if mode == "preserve" or file_eol is None:
        return EditLineEndings(search, replace, file_eol, False)

    resolved_search = search
    idx = content.find(search)
    if idx == -1:
        styles = [file_eol]
        if has_mixed_line_endings(content):
            styles.append("\n" if file_eol == "\r\n" else "\r\n")
        for eol in styles:
            candidate = convert_line_endings(search, eol)
            if candidate == search:
                continue
            found = content.find(candidate)
            if found != -1:
                resolved_search, idx = candidate, found
                break
    if idx == -1:
        # Miss in every style: hand the anchor back untouched for the miss report.
        return EditLineEndings(search, replace, file_eol, False)

    target_eol = (
        detect_line_ending(resolved_search)
        or line_ending_at(content, idx)
        or file_eol
    )
    resolved_replace = convert_line_endings(replace, target_eol)
    normalized = resolved_search != search or resolved_replace != replace
    return EditLineEndings(resolved_search, resolved_replace, file_eol, normalized)
