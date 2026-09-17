"""Line-ending policy for literal edit anchors (fs_apply_file_edits, fs_search_and_replace).

Models write anchors with LF while files on Windows keep CRLF; the tools match
literally, so the anchor is resolved here: literal first, then converted to the
file's style (both styles when the file mixes them). The replacement follows the
line ending of the matched region — a multi-line anchor carries its own, a
single-line anchor takes the line it sits on — so an edit never introduces a
second style into that region.

The line-ending style is a property of the file: edit tools never change it and
never rewrite file content. Converting a file is the job of the write tool
(newline=lf|crlf) or a shell command.
"""

from __future__ import annotations

from dataclasses import dataclass

from gc_core.constants.tool_names import FS_WRITE_FILE_NAME
from gc_core.utils.file_text import (
    convert_line_endings,
    detect_line_ending,
    has_mixed_line_endings,
    line_ending_at,
)

LINE_ENDING_CHANGE_REFUSED = "line_ending_change_refused"


@dataclass(frozen=True)
class EditLineEndings:
    search: str
    replace: str
    file_line_ending: str | None  # "\r\n" | "\n" | None when the file has no newlines
    first_index: int  # -1 when the anchor was not found in any style
    eol_adapted: bool  # search and/or replace bytes were converted to the matched style


def line_ending_change_refused_error() -> dict[str, str]:
    return {"type": LINE_ENDING_CHANGE_REFUSED}


def line_ending_change_refused_message() -> str:
    return (
        "The replacement differs from the matched text only by line endings, "
        "which this tool keeps as in the file. To change the file's line-ending "
        f"style, rewrite it with {FS_WRITE_FILE_NAME} (newline=lf|crlf) or a shell command."
    )


def match_span(content: str, search: str, idx: int) -> tuple[int, int, int]:
    fragment = content[idx : idx + len(search)]
    start_line = content.count("\n", 0, idx) + 1
    end_line = start_line + fragment.count("\n")
    return idx, start_line, end_line


def collect_literal_matches(
    content: str, search: str, first_index: int
) -> list[tuple[int, int, int]]:
    """All non-overlapping matches of ``search``, starting from a known first hit."""
    if first_index < 0:
        return []
    entries = [match_span(content, search, first_index)]
    pos = first_index + len(search)
    while True:
        idx = content.find(search, pos)
        if idx == -1:
            break
        entries.append(match_span(content, search, idx))
        pos = idx + len(search)
    return entries


def resolve_edit_line_endings(content: str, search: str, replace: str) -> EditLineEndings:
    file_eol = detect_line_ending(content)
    if file_eol is None:
        idx = content.find(search)
        return EditLineEndings(search, replace, None, idx, False)

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
        return EditLineEndings(search, replace, file_eol, -1, False)

    target_eol = (
        detect_line_ending(resolved_search)
        or line_ending_at(content, idx)
        or file_eol
    )
    resolved_replace = convert_line_endings(replace, target_eol)
    eol_adapted = resolved_search != search or resolved_replace != replace
    return EditLineEndings(resolved_search, resolved_replace, file_eol, idx, eol_adapted)
