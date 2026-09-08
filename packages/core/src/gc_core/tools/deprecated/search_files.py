import json
import bisect
import functools
import os
import re
from typing import Optional, List, Dict, Any, Tuple
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.glob_to_regex import compile_glob
from gc_core.utils.tool_report_paths import tool_report_abspath
from gc_core.utils.fs_size_limits import check_file_size, fs_read_max_bytes
from gc_core.utils.file_text import decode_text_best_effort, decode_text_bytes
from gc_core.tools.fs.agent_hidden import is_agent_hidden
from gc_core.security.read_guard import enforce_read_guard, parse_read_guard_config, check_read_path_allowed, ReadGuardViolation, _resolve_skills_root
from gc_core.security.path_policy import resolve_tool_context, _extract_config_dict


class SearchFiles(AbstractSimpleTool):
    """Paginated grep-style search; default first page mirrors fs_read_file preview (500 items)."""

    default_limit = 500
    # Cap skipped entries in the tool envelope so huge trees don't flood the model.
    default_skipped_limit = 20
    max_match_content_chars = 2000
    _literal_word_re = re.compile(r"[a-zA-Z_а-яА-Я][a-zA-Z0-9_а-яА-Я]+")

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return {
            "type": "function",
            "name": "fs_search_files",
            "description": (
                "Searches for a regex pattern in file contents (not for listing files by name). "
                "`regex` is required — this tool does not list files by glob/name alone. "
                "Full-file regex: matches may span newlines; '.' matches newline; '^'/'$' match "
                "line boundaries — Python re DOTALL+MULTILINE. "
                "`path` may be a directory (recursive search, optional file_pattern filter) "
                "or a single file. Returns paginated hits: metadata includes total_matches, "
                "offset, limit, truncated, next_offset, then matches (slice for this page). "
                "Each hit has line (start), end_line, line_content, context. "
                "Field count is the number of matches in this response (same as len(matches)); "
                "total_matches is the full hit count for the search scope. "
                "Files too large for fs_tools.max_read_bytes, binary, or unreadable are listed in "
                "`skipped` with a reason (`file_too_large` / `is_binary` / `read_failed` / …) — "
                "they are not searched; do not treat missing hits "
                "there as 'no match'. `skipped_total` is the full skip count; the list may be "
                f"capped (default {cls.default_skipped_limit}) with `skipped_truncated`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory to search recursively, or a single file to search. "
                            "Must be a valid absolute path."
                        ),
                    },
                    "regex": {
                        "type": "string",
                        "description": (
                            "Required. Regex matched against file contents (DOTALL+MULTILINE: "
                            "patterns may span newlines; '.' matches newline; "
                            "'^'/'$' match line boundaries). "
                            "Keep patterns bounded — avoid unbounded .* across huge files. "
                            "Avoid unsupported features like backreferences or "
                            "variable-length lookarounds. "
                            "Use double backslashes (e.g., \\d, \\s). "
                            "Cannot be omitted: this tool always searches contents."
                        ),
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": (
                            "Optional glob to narrow which files are content-searched when path "
                            "is a directory (e.g., '*.py', 'src/**/*.rs'). Not a standalone file "
                            "finder — always pair with regex. Ignored when path is a file. "
                            "When unset, all files under the directory are searched."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Optional 1-based index of the first match to return (among all matches ordered by walk). "
                            f"Defaults to 1 together with limit. When both offset and limit are omitted, offsets 1 "
                            f"and uses default limit ({cls.default_limit})."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Optional maximum number of matches to return starting from offset. "
                            f"If omitted but offset is provided, defaults to {cls.default_limit}. "
                            "If both offset and limit are omitted, defaults to offset=1 and the same limit."
                        ),
                    },
                    "encoding": {
                        "type": "string",
                        "description": (
                            "Optional text encoding. Omit for UTF-8 (default; also UTF-16 with BOM). "
                            "Pass encoding= to search files in cp1251, koi8-r, latin-1, "
                            "utf-8-sig, utf-16, utf-16-le, utf-16-be "
                            "(also windows-1251, iso-8859-1). When set, all searched files are "
                            "decoded with this encoding; files that fail to decode are skipped."
                        ),
                    },
                },
                "required": ["path", "regex"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = '''Tool name: fs_search_files  
Tool description: Searches for a regex pattern in file contents (not for listing files by name). regex is required; does not list files by glob/name alone.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "Directory to search recursively, or a single file."}, "regex": {"type": "string", "description": "Required. Regex matched against the whole file (DOTALL+MULTILINE: patterns may span newlines; '.' matches newline; '^'/'$' are line boundaries). Keep patterns bounded."}, "file_pattern": {"type": "string", "description": "Optional glob to narrow which files are content-searched (e.g., \'*.py\'). Always pair with regex — not a standalone name listing."}, "offset": {"type": "integer", "description": "1-based index of the first match to return."}, \"limit\": {\"type\": \"integer\", \"description\": \"Max matches returned for this page.\"}, \"encoding\": {\"type\": \"string\", \"description\": \"Optional text encoding (e.g. cp1251, koi8-r, utf-16-le). Omit for UTF-8 default.\"}}, \"required\": [\"path\", \"regex\"], \"additionalProperties\": false}

Usage:
- `path` may be a **directory** (search all files recursively) or a **single file**.
- When `path` is a directory, optionally narrow files with **file_pattern** (glob). `file_pattern` alone is not enough — `regex` is always required. This tool does not list files by name/glob alone.
- Matches using a **regex** over the **whole file** (DOTALL+MULTILINE: patterns may span newlines; `.` matches newline; `^`/`$` match line boundaries). Keep patterns bounded.
- Returns a **paginated** list of matches with file paths, **`line`** (start), **`end_line`**, `line_content`, and ±5 lines of `context`.
- **`count`** = matches in **this response** (page size). **`total_matches`** = all hits in the scan.
- If **`truncated`** is true, call again with **`offset` = `next_offset`** (and same or new `limit`) to get more.
- Omitting **`offset`** and **`limit`** is equivalent to the first page: offset 1 and default **`limit`**.
- Returns an empty **`matches`** list if none found.
- Files skipped (too large / binary / unreadable) appear in **`skipped`** with a reason (`file_too_large`, `is_binary`, `read_failed`, …); **`skipped_total`** is the full count (list may be capped, see **`skipped_truncated`**). A skip is not the same as "no match".
- Prefer a dedicated file-read tool when you need the full file; use this tool to locate matches.

Important Usage Rules
- **ALWAYS** use `fs_search_files` for searching text or code inside files.
- This tool does **not** list/find files by name or glob alone — `regex` is always required.
- **NEVER** use external tools like `grep`, `rg`, or shell commands — this tool is optimized and permission-safe.
- Always provide a **full absolute path** for the `path` parameter.
- The `regex` must be a valid Rust-compatible pattern. Avoid unsafe features:
  - No backreferences (e.g., `\1`, `\2`)
  - No variable-length lookarounds (e.g., `(?<=a.*)b`)
  - No recursive or deeply nested patterns
- Prefer tight patterns; unbounded `.*` can span huge regions of a file.
- Use **simple, efficient regex patterns** to avoid performance issues or excessive results.
- Always escape special sequences with **double backslashes**: use `\\d`, `\\s`, `\\w`, `\\.` instead of `\\d`, `\\s`, etc.
- Use `file_pattern` to narrow scope (e.g., `"*.ts"`, `"tests/**/*"`) when searching a directory.

Supported Glob Patterns in `file_pattern`
- `*` — Matches any sequence of characters except `/`.
- `**` — Matches any sequence, including `/` (recursive across directories).
- `?` — Matches any single character except `/`.
- `[seq]` — Matches any one character in `seq` (e.g., `[abc]`).
- `[!seq]` — Matches any character **not** in `seq` (e.g., `[!0-9]`).

Examples
Find all function definitions in Python files:
{
 "path": "/home/user/project",
 "regex": "def\\s+\\w+\\s*\\(",
 "file_pattern": "*.py"
}
   
Search one known file (no shell grep):
{
 "path": "/home/user/project/tests/test_foo.py",
 "regex": "def fake_"
}

Search for error logs in Rust source files:
{
  "path": "/home/user/project/src",
  "regex": "log.*Error",
  "file_pattern": "**/*.rs"
}
'''
        return description

    def _extract_literals(self, pattern: str) -> Tuple[str, ...]:
        """Буквенные фрагменты паттерна для быстрой фильтрации строк (OR, не AND)."""
        seen: set[str] = set()
        literals: List[str] = []
        for match in self._literal_word_re.finditer(pattern):
            word = match.group(0)
            if word not in seen:
                seen.add(word)
                literals.append(word)
        return tuple(literals)

    @staticmethod
    def _line_start_offsets(lines: List[str]) -> List[int]:
        """Byte/char offsets of the start of each line (aligned with ``splitlines``)."""
        starts: List[int] = []
        pos = 0
        for line in lines:
            starts.append(pos)
            pos += len(line)
        return starts

    @staticmethod
    def _offset_to_line(line_starts: List[int], offset: int, text_len: int) -> int:
        """1-based line number of the character at ``offset`` (clamped)."""
        if not line_starts:
            return 1
        if offset <= 0:
            return 1
        if offset >= text_len:
            return len(line_starts)
        return bisect.bisect_right(line_starts, offset)

    def _clip_match_content(self, content: str) -> str:
        max_chars = self.max_match_content_chars
        if len(content) <= max_chars:
            return content
        return (
            content[:max_chars]
            + f"...[match_truncated: showing first {max_chars} of "
            f"{len(content)} chars]"
        )

    def _build_match_context(
        self,
        lines: List[str],
        start_line: int,
        end_line: int,
        context_num: int = 5,
    ) -> str:
        """±context_num around the match; large spans → head + cut marker + tail."""
        n = len(lines)
        # Keep a contiguous window when the match itself is small.
        if end_line - start_line <= 2 * context_num:
            ctx_lo = max(0, start_line - 1 - context_num)
            ctx_hi = min(n, end_line + context_num)
            return "".join(lines[ctx_lo:ctx_hi])

        head_lo = max(0, start_line - 1 - context_num)
        head_hi = min(n, start_line + context_num)
        tail_lo = max(0, end_line - 1 - context_num)
        tail_hi = min(n, end_line + context_num)
        if tail_lo <= head_hi:
            return "".join(lines[head_lo:tail_hi])

        omitted = tail_lo - head_hi
        marker = (
            f"...[context_truncated: match spans lines {start_line}-{end_line}; "
            f"omitted {omitted} lines between context windows]...\n"
        )
        return "".join(lines[head_lo:head_hi]) + marker + "".join(lines[tail_lo:tail_hi])

    def _search_text(
        self, file_path: str, text: str, regex: str, context_num: int = 5
    ) -> List[Dict[str, Any]]:
        try:
            compiled_regex = re.compile(regex, re.DOTALL | re.MULTILINE)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{regex}': {str(e)}")

        literal_hints = self._extract_literals(regex)
        if literal_hints and not any(hint in text for hint in literal_hints):
            return []

        lines = text.splitlines(keepends=True)
        line_starts = self._line_start_offsets(lines)
        text_len = len(text)
        results: List[Dict[str, Any]] = []
        report_path = tool_report_abspath(file_path, workspace=self._workspace)

        for match in compiled_regex.finditer(text):
            start = match.start()
            end = match.end()
            start_line = self._offset_to_line(line_starts, start, text_len)
            end_line = (
                start_line
                if end <= start
                else self._offset_to_line(line_starts, end - 1, text_len)
            )
            results.append(
                {
                    "file": report_path,
                    "line": start_line,
                    "end_line": end_line,
                    "line_content": self._clip_match_content(match.group(0)),
                    "context": self._build_match_context(
                        lines, start_line, end_line, context_num
                    ),
                }
            )
        return results

    def _result(self, 
        *,
        path: str,
        regex: str,
        file_pattern: Optional[str],
        ok: bool,
        matches: Optional[List[Dict[str, Any]]] = None,
        error: Optional[Dict[str, Any]] = None,
        total_matches: Optional[int] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        truncated: Optional[bool] = None,
        next_offset: Optional[int] = None,
        skipped: Optional[List[Dict[str, Any]]] = None,
        skipped_total: Optional[int] = None,
        skipped_truncated: Optional[bool] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": SearchFiles.registered_tool_name(),
            "ok": ok,
            "path": tool_report_abspath(path, workspace=self._workspace),
            "regex": regex,
            "file_pattern": file_pattern,
            "error": error,
            "matches": matches if matches is not None else [],
        }
        if skipped is not None:
            payload["skipped"] = skipped
            payload["skipped_total"] = skipped_total if skipped_total is not None else len(skipped)
            payload["skipped_truncated"] = bool(skipped_truncated)
        if ok and total_matches is not None:
            payload["total_matches"] = total_matches
            payload["count"] = len(matches) if matches is not None else 0
            payload["offset"] = offset
            payload["limit"] = limit
            payload["truncated"] = bool(truncated)
            payload["next_offset"] = next_offset
        elif not ok:
            payload["total_matches"] = None
            payload["count"] = 0
            payload["offset"] = None
            payload["limit"] = None
            payload["truncated"] = None
            payload["next_offset"] = None

        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def _resolve_slice(
        cls, total: int, offset: Optional[int], limit: Optional[int]
    ) -> Tuple[bool, Optional[Dict[str, Any]], int, int, int]:
        """Return ok, error, start_0, limit_eff, offset_effective."""
        if offset is None and limit is None:
            off_eff = 1
            lim_eff = cls.default_limit
        elif offset is None:
            off_eff = 1
            lim_eff = limit if limit is not None else cls.default_limit
        elif limit is None:
            off_eff = offset
            lim_eff = cls.default_limit
        else:
            off_eff = offset
            lim_eff = limit

        if off_eff < 1:
            return False, {"type": "invalid_argument", "field": "offset", "message": "offset must be >= 1"}, 0, 0, 0
        if lim_eff < 1:
            return False, {"type": "invalid_argument", "field": "limit", "message": "limit must be >= 1"}, 0, 0, 0

        start_0 = off_eff - 1
        if start_0 >= total:
            start_0 = total

        return True, None, start_0, lim_eff, off_eff

    async def acall(self, *args, **kwargs) -> str:
        """Инлайн на loop: нативного async у файлов нет (docs/async.md)."""
        return self(*args, **kwargs)

    @enforce_read_guard(path_arg="path")
    def __call__(
        self,
        path: str,
        regex: str,
        file_pattern: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        encoding: Optional[str] = None,
        **kwargs,
    ) -> str:
        file_pattern = coalesce_optional_string(file_pattern)

        encoding = coalesce_optional_string(encoding)

        _result = functools.partial(
            self._result,
            path=path if isinstance(path, str) else str(path),
            regex=regex if isinstance(regex, str) else "",
            file_pattern=file_pattern,
        )

        if not isinstance(path, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "path"})
        if not isinstance(regex, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "regex"})
        if file_pattern is not None and not isinstance(file_pattern, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "file_pattern"})
        if offset is not None and not isinstance(offset, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "offset"})
        if limit is not None and not isinstance(limit, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "limit"})
        if encoding is not None and not isinstance(encoding, str):
            return _result(ok=False, error={"type": "invalid_argument", "field": "encoding"})

        try:
            re.compile(regex, re.DOTALL | re.MULTILINE)
        except re.error as e:
            return _result(
                ok=False,
                error={
                    "type": "invalid_argument",
                    "field": "regex",
                    "message": f"Invalid regex: {e}",
                },
            )

        if not self._backend.exists(path):
            return _result(ok=False, error={"type": "not_found"})

        # Per-entry read_guard: пропускаем dir'и/файлы которые денилисчены,
        # включая symlinks ведущие наружу launch_root. Резолвим конфиг один раз.
        _rg_context = resolve_tool_context(self)
        _rg_config_dict = _extract_config_dict(self, getattr(_rg_context, "ctx", None), {})
        _rg_cfg = parse_read_guard_config(_rg_config_dict)
        _rg_root = _rg_context.launch_root if _rg_cfg.enabled else None
        _rg_active = _rg_cfg.enabled
        _rg_extra = None
        if _rg_active:
            _skills_root = _resolve_skills_root(_rg_config_dict)
            if _skills_root:
                _rg_extra = [_skills_root]

        def _rg_pass(p: str) -> bool:
            if is_agent_hidden(os.path.basename(os.path.normpath(p))):
                return False
            if not _rg_active:
                return True
            try:
                check_read_path_allowed(
                    p,
                    launch_root=_rg_root,
                    cfg=_rg_cfg,
                    extra_allowed_paths=_rg_extra,
                )
            except (ReadGuardViolation, ValueError):
                return False
            return True

        results: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        max_bytes = fs_read_max_bytes(self._config_dict)

        def _skip(abs_path: str, reason: str, **extra: Any) -> None:
            entry: Dict[str, Any] = {
                "path": tool_report_abspath(abs_path, workspace=self._workspace),
                "reason": reason,
            }
            entry.update(extra)
            skipped.append(entry)

        def _load_search_text(abs_path: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
            """Return (text, None) or (None, error dict with type/message for skip or tool error)."""
            try:
                raw = self._backend.read_bytes(abs_path)
            except OSError as e:
                return None, {"type": "read_failed", "message": str(e)}

            decoded = decode_text_bytes(raw, encoding=encoding)
            if decoded.ok:
                return decoded.text or "", None

            err = decoded.error or {
                "type": "read_failed",
                "message": "Failed to decode file.",
            }
            err_type = err.get("type") or "read_failed"
            if err_type == "is_binary":
                return None, {
                    "type": "is_binary",
                    "message": err.get("message")
                    or "File looks binary; not searchable as text.",
                }

            # Non-UTF8 text: best-effort decode for discovery (same idea as backend.read).
            # Only when no explicit encoding was requested — with an explicit encoding,
            # a decode failure means the file is genuinely not in that encoding.
            if encoding is None:
                text = decode_text_best_effort(raw)
                if text is not None:
                    return text, None
            return None, {
                "type": err_type,
                "message": err.get("message") or "Failed to decode file.",
            }

        def _search_file_contents(abs_path: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
            text, err = _load_search_text(abs_path)
            if err is not None:
                return None, err
            assert text is not None
            return self._search_text(abs_path, text, regex), None

        def _search_one(abs_path: str) -> Optional[List[Dict[str, Any]]]:
            try:
                size = self._backend.getsize(abs_path)
            except OSError as e:
                _skip(abs_path, "stat_failed", message=str(e))
                return None
            size_err = check_file_size(size, max_bytes, operation="search_files")
            if size_err is not None:
                _skip(
                    abs_path,
                    "file_too_large",
                    actual_bytes=size,
                    max_bytes=max_bytes,
                )
                return None
            hits, err = _search_file_contents(abs_path)
            if err is not None:
                _skip(
                    abs_path,
                    err.get("type") or "read_failed",
                    message=err.get("message"),
                )
                return None
            return hits

        if self._backend.is_file(path):
            # Single-file search (models often pass a file like `rg pattern file.py`).
            # file_pattern does not apply — the path already names the file.
            try:
                size = self._backend.getsize(path)
            except OSError:
                return _result(ok=False, error={"type": "not_found"})
            size_err = check_file_size(size, max_bytes, operation="search_files")
            if size_err is not None:
                return _result(ok=False, error=size_err)
            hits, err = _search_file_contents(path)
            if err is not None:
                return _result(ok=False, error=err)
            results = hits or []
        elif self._backend.is_dir(path):
            file_matcher = self._create_file_matcher(file_pattern, base_path=path)
            try:
                file_iter = self._backend.iter_files(path, should_descend=_rg_pass)
            except FileNotFoundError:
                return _result(ok=False, error={"type": "not_found"})

            for relpath in file_iter:
                abs_path = self._backend.join(path, relpath)
                if file_pattern and not file_matcher(abs_path, path):
                    continue
                if not _rg_pass(abs_path):
                    continue
                file_result = _search_one(abs_path)
                if file_result:
                    results += file_result
        else:
            return _result(ok=False, error={"type": "not_a_directory"})

        total_matches = len(results)
        slice_ok, slice_err, start_0, lim_eff, off_eff = self._resolve_slice(
            total_matches, offset, limit
        )
        if not slice_ok:
            return _result(ok=False, error=slice_err)

        end_0 = min(start_0 + lim_eff, total_matches)
        page = results[start_0:end_0]
        truncated = end_0 < total_matches
        next_offset = end_0 + 1 if truncated else None

        return _result(
            ok=True,
            matches=page,
            error=None,
            total_matches=total_matches,
            offset=off_eff,
            limit=lim_eff,
            truncated=truncated,
            next_offset=next_offset,
            skipped=skipped[: self.default_skipped_limit] if skipped else None,
            skipped_total=len(skipped) if skipped else None,
            skipped_truncated=(
                len(skipped) > self.default_skipped_limit if skipped else None
            ),
        )

    def _create_file_matcher(self, file_pattern: Optional[str], base_path: str):
        """Creates a matcher function based on glob-like file_pattern."""
        if not file_pattern:
            return lambda fp, bp: True

        glob_re = compile_glob(file_pattern)

        def matches(filepath: str, base_path: str) -> bool:
            filepath = filepath.replace(os.sep, '/')
            relative_path = os.path.relpath(filepath, base_path)
            return bool(glob_re.match(relative_path))

        return matches
