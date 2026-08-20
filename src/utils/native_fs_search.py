"""Native find/grep recipes for fs tools (capability-gated).

Correctness > speed. On any anomaly callers fall back to the Python path.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from backends.capabilities import parse_find_printf_lines
from backends.walk_util import iter_walk_from_flat
from utils.fs_search_walk import effective_exclude_dir_names
from utils.pattern_native_gate import NativeGrepPlan, is_safe_grep_include

log = logging.getLogger(__name__)

_GREP_LINE_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>[0-9]+):(?P<text>.*)$")
_GREP_CLEAN_ENV = {
    "GREP_OPTIONS": "",
    "GREP_COLORS": "",
    "GREP_COLOR": "",
}


@dataclass
class NativeGrepResult:
    matches: List[Dict[str, Any]]
    truncated: bool
    hit_capture_cap: bool
    backend: str = "grep"


def can_native_list(backend: Any) -> bool:
    try:
        return bool(getattr(backend, "has_find", False))
    except Exception:
        return False


def can_native_grep_content(
    backend: Any,
    *,
    plan: Optional[NativeGrepPlan],
    exclude_dir_names: Sequence[str],
    call_exclude_dirs: Optional[Sequence[str]],
    file_pattern: Optional[str],
    exclude_nested_dot_dirs: bool = True,
) -> bool:
    """Filters-in-recipe gate (I1–I4). Partial excludes → False."""
    if plan is None:
        return False
    try:
        if not getattr(backend, "has_grep", False):
            return False
    except Exception:
        return False

    # Call-time abs excludes cannot be expressed as basename prune/--exclude-dir.
    if call_exclude_dirs:
        return False

    names = [n for n in (exclude_dir_names or []) if n]
    need_prune = bool(names) or bool(exclude_nested_dot_dirs)
    if need_prune:
        # Express hard/nested-dot skips via find -prune (needs usable find).
        try:
            if not getattr(backend, "has_find", False):
                return False
        except Exception:
            return False

    if file_pattern is not None:
        if not is_safe_grep_include(file_pattern):
            return False
        # With find prune path we use find -name for include (no grep_include required).
        # Plain grep -R path still needs grep_include when no find prune.
        if not need_prune:
            try:
                if not getattr(backend, "grep_include", False):
                    return False
            except Exception:
                return False

    return True


def native_find_dump(
    backend: Any,
    path: str,
    *,
    maxdepth: Optional[int] = None,
) -> Optional[List[Tuple[str, bool]]]:
    """Run find -printf wire; return rows or None on failure."""
    argv: List[str] = ["find", path, "-mindepth", "1"]
    if maxdepth is not None:
        argv.extend(["-maxdepth", str(int(maxdepth))])
    argv.extend(["-printf", "%P\t%y\n"])
    try:
        res = backend.exec_argv(argv, timeout=120, env=dict(_GREP_CLEAN_ENV))
    except Exception as exc:
        log.debug("native find dump failed: %s", exc)
        return None
    if getattr(res, "timed_out", False):
        return None
    if res.return_code != 0:
        return None
    return parse_find_printf_lines(res.stdout or "")


def native_list_entries(
    backend: Any,
    path: str,
    *,
    max_depth: int,
    max_entries: Optional[int],
    file_matcher: Optional[Callable[[str], bool]],
    is_hidden_name: Callable[[str], bool],
    read_guard_pass: Callable[[str], bool],
    make_entry: Callable[[str, bool], Dict[str, Any]],
) -> Optional[Tuple[List[Dict[str, Any]], bool, str]]:
    """Native list via find dump + Python order/filters (parity with python path).

    Order must match ``listdir`` (depth 0) / ``iter_walk`` (depth ≥ 1): not raw
    find inode order. Agent-hidden dirs are pruned so children are never listed.

    Returns ``(entries, truncated, listing_backend)`` or None → Python fallback.
    """
    if not can_native_list(backend):
        return None
    # list_files: max_depth=0 → immediate children → find -maxdepth 1
    depth = int(max_depth)
    find_maxdepth = max(1, depth + 1)

    rows = native_find_dump(backend, path, maxdepth=find_maxdepth)
    if rows is None:
        return None

    rows = [(rel.replace("\\", "/"), is_dir) for rel, is_dir in rows]

    if depth == 0:
        # Parity with backend.listdir: alphabetical by name (not dirs-first).
        ordered: List[Tuple[str, bool]] = sorted(
            ((rel, is_dir) for rel, is_dir in rows if "/" not in rel),
            key=lambda item: item[0],
        )
    else:
        def _should_descend(abs_path: str) -> bool:
            base = os.path.basename(abs_path.rstrip("/") or abs_path)
            if is_hidden_name(base):
                return False
            if not read_guard_pass(abs_path):
                return False
            rel = os.path.relpath(abs_path, path).replace(os.sep, "/")
            if rel in (".", ""):
                return True
            if rel.count("/") >= depth:
                return False
            return True

        ordered = list(
            iter_walk_from_flat(
                rows,
                should_descend=_should_descend,
                join=backend.join,
                root=path,
            )
        )

    result: List[Dict[str, Any]] = []
    hit_limit = False
    scan_max = (max_entries + 1) if max_entries is not None else None

    for rel, is_dir in ordered:
        base = os.path.basename(rel)
        if is_hidden_name(base):
            continue
        if file_matcher is not None and not file_matcher(rel):
            continue
        abs_path = backend.join(path, rel)
        if not read_guard_pass(abs_path):
            continue
        result.append(make_entry(abs_path, is_dir))
        if scan_max is not None and len(result) >= scan_max:
            hit_limit = True
            break

    if max_entries is not None and len(result) > max_entries:
        result = result[:max_entries]
        hit_limit = True

    return result, hit_limit, "find"


def native_find_by_name_rows(
    backend: Any,
    path: str,
) -> Optional[List[Tuple[str, bool]]]:
    """Full-tree find dump for by-name (Python matcher applies excludes/glob)."""
    if not can_native_list(backend):
        return None
    return native_find_dump(backend, path, maxdepth=None)


def parse_grep_match_lines(
    stdout: str,
    *,
    root: str,
    join: Callable[[str, str], str],
    report_path: Callable[[str], str],
) -> Optional[List[Dict[str, Any]]]:
    """Parse ``PATH:LINENO:TEXT``. Unparseable → None (fallback)."""
    matches: List[Dict[str, Any]] = []
    for raw in (stdout or "").splitlines():
        if not raw:
            continue
        if "Binary file" in raw or "\x1b[" in raw:
            return None
        m = _GREP_LINE_RE.match(raw)
        if not m:
            return None
        path = m.group("path")
        if ":" in path:
            return None
        try:
            line_no = int(m.group("line"))
        except ValueError:
            return None
        text = m.group("text")
        if not os.path.isabs(path):
            abs_p = join(root, path)
        else:
            abs_p = path
        matches.append(
            {
                "file": report_path(abs_p),
                "line": line_no,
                "end_line": line_no,
                "line_content": text,
                "context": text + ("\n" if not text.endswith("\n") else ""),
            }
        )
    return matches


def _build_find_prune_argv(
    path: str,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    print0: bool = True,
) -> List[str]:
    """find PATH with basename prunes → file paths.

    ``-mindepth 1`` ensures the start path itself is never pruned when its
    basename is a hard-exclude name (search-inside-junk-dir is allowed).
    """
    argv: List[str] = ["find", path, "-mindepth", "1"]
    prune_parts: List[List[str]] = []
    for name in exclude_dir_names:
        if not name:
            continue
        prune_parts.append(["-type", "d", "-name", name])
    if exclude_nested_dot_dirs:
        prune_parts.append(["-type", "d", "-name", ".*"])

    if prune_parts:
        argv.append("(")
        for i, part in enumerate(prune_parts):
            if i:
                argv.append("-o")
            argv.extend(part)
        argv.extend([")", "-prune", "-o"])

    argv.extend(["-type", "f"])
    if file_pattern:
        argv.extend(["-name", file_pattern.strip()])
    if print0:
        argv.append("-print0")
    else:
        argv.append("-print")
    return argv


def count_native_candidates(
    backend: Any,
    path: str,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
) -> Optional[int]:
    """Count files find would search. None on failure."""
    find_argv = _build_find_prune_argv(
        path,
        exclude_dir_names=exclude_dir_names,
        exclude_nested_dot_dirs=exclude_nested_dot_dirs,
        file_pattern=file_pattern,
        print0=False,
    )
    # Count newlines in -print output (paths should not contain newlines).
    find_s = " ".join(shlex.quote(a) for a in find_argv)
    cmd = f"{find_s} | wc -l"
    try:
        res = backend.exec(cmd, timeout=120, env=dict(_GREP_CLEAN_ENV))
    except Exception:
        return None
    if res.return_code not in (0, None):
        return None
    try:
        return int((res.stdout or "0").strip().split()[0])
    except (ValueError, IndexError):
        return None


def run_native_grep(
    backend: Any,
    path: str,
    plan: NativeGrepPlan,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    max_matches: int,
    report_path: Callable[[str], str],
    is_dir_target: bool,
) -> Optional[NativeGrepResult]:
    """Execute grep recipe (optionally via find prune). None → Python fallback."""
    names = [n for n in exclude_dir_names if n]
    need_prune = is_dir_target and (bool(names) or exclude_nested_dot_dirs)

    grep_argv: List[str] = ["grep", "-n", "--color=never"]
    grep_argv.extend(["--no-messages", "--binary-files=without-match"])
    if plan.case_insensitive:
        grep_argv.append("-i")
    if plan.fixed:
        grep_argv.append("-F")
    else:
        grep_argv.append("-E")
    grep_argv.extend(["--", plan.pattern])

    try:
        if need_prune:
            # find -print0 | xargs -0 -r grep ...  (GNU xargs -r: no run if empty)
            find_argv = _build_find_prune_argv(
                path,
                exclude_dir_names=names,
                exclude_nested_dot_dirs=exclude_nested_dot_dirs,
                file_pattern=file_pattern,
            )
            # Single shell pipeline with quoted argv pieces — Harbor/local POSIX.
            find_s = " ".join(shlex.quote(a) for a in find_argv)
            grep_s = " ".join(shlex.quote(a) for a in grep_argv)
            # xargs -r is GNU; on macOS without -r empty input may still call grep once.
            cmd = f"{find_s} | xargs -0 -r {grep_s}"
            res = backend.exec(cmd, timeout=120, env=dict(_GREP_CLEAN_ENV))
        elif is_dir_target:
            argv = ["grep", "-R", "-n", "--color=never"]
            argv.extend(["--no-messages", "--binary-files=without-match"])
            if plan.case_insensitive:
                argv.append("-i")
            if plan.fixed:
                argv.append("-F")
            else:
                argv.append("-E")
            if file_pattern:
                argv.append(f"--include={file_pattern.strip()}")
            argv.extend(["--", plan.pattern, path])
            res = backend.exec_argv(argv, timeout=120, env=dict(_GREP_CLEAN_ENV))
        else:
            # single file
            argv = list(grep_argv) + [path]
            res = backend.exec_argv(argv, timeout=120, env=dict(_GREP_CLEAN_ENV))
    except Exception as exc:
        log.debug("native grep failed: %s", exc)
        return None

    if getattr(res, "timed_out", False):
        return None

    rc = res.return_code
    stdout = res.stdout or ""
    stderr = res.stderr or ""
    hit_cap = bool(getattr(res, "stdout_hit_capture_cap", False))

    # xargs may return 123 if grep returns 1 on some chunks — treat carefully.
    if rc is not None and rc >= 2:
        # 123 from xargs: some invocations failed
        if rc not in (123,):
            return None
        # still try parse if stdout looks clean
        if stderr.strip() and "Binary" not in stderr:
            return None

    if rc == 1:
        if stderr.strip():
            return None
        if stdout.strip():
            return None
        return NativeGrepResult(matches=[], truncated=False, hit_capture_cap=hit_cap)

    if rc not in (0, 123, None):
        return None
    if rc == 0 and not stdout.strip() and not hit_cap:
        # empty success — ok for xargs -r with zero files
        return NativeGrepResult(matches=[], truncated=False, hit_capture_cap=hit_cap)

    lines = stdout.splitlines()
    need = max_matches + 1
    truncated = False
    if len(lines) > need:
        lines = lines[:need]
        truncated = True
    elif hit_cap:
        truncated = True

    parsed = parse_grep_match_lines(
        "\n".join(lines),
        root=path,
        join=backend.join,
        report_path=report_path,
    )
    if parsed is None:
        return None

    if len(parsed) > max_matches:
        parsed = parsed[:max_matches]
        truncated = True

    return NativeGrepResult(
        matches=parsed,
        truncated=truncated,
        hit_capture_cap=hit_cap,
    )


def hard_exclude_names_for_grep(extra: Optional[Sequence[str]] = None) -> List[str]:
    """Basenames for prune/--exclude-dir (hard floor ∪ config extras)."""
    return sorted(effective_exclude_dir_names(extra))
