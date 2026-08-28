"""Native grep recipes for fs content search (capability-gated).

List / by-name enumeration is NOT here: tools use backend.walk / listdir /
iter_walk (Harbor private walk channel ~50M fail-closed; local os.walk).
Tool-exec (60k) must never dump a full tree.

Correctness > speed. On any anomaly callers fall back to the Python path.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from utils.find_in_files_scan_mode import Needles
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
    # Why the recipe could not answer (``native_error`` / ``native_timeout``);
    # None on success. Callers fall back to the Python path and tell the agent.
    failure: Optional[str] = None


def native_grep_refusal(
    backend: Any,
    *,
    plan: Optional[NativeGrepPlan],
    exclude_dir_names: Sequence[str],
    call_exclude_dirs: Optional[Sequence[str]],
    file_pattern: Optional[str],
    exclude_nested_dot_dirs: bool = True,
) -> Optional[str]:
    """Filters-in-recipe gate (I1–I4): why native cannot answer, None when it can.

    The reason travels to the agent as ``fallback_reason`` — the Python path
    reads every candidate file from the host, and silence would hide that cost.
    Partial excludes → refuse.
    """
    if plan is None:
        return "pattern_not_native"
    try:
        if not getattr(backend, "has_grep", False):
            return "no_grep"
    except Exception:
        return "no_grep"

    names = [n for n in (exclude_dir_names or []) if n]
    # Call-time absolute excludes ride the same recipe as ``find -path … -prune``.
    need_prune = bool(names) or bool(exclude_nested_dot_dirs) or bool(call_exclude_dirs)
    if need_prune:
        # Express hard/nested-dot/call-time skips via find -prune (needs usable find).
        try:
            if not getattr(backend, "has_find", False):
                return "no_find"
        except Exception:
            return "no_find"

    if file_pattern is not None:
        if not is_safe_grep_include(file_pattern):
            return "file_pattern_not_native"
        # With find prune path we use find -name for include (no grep_include required).
        # Plain grep -R path still needs grep_include when no find prune.
        if not need_prune:
            try:
                if not getattr(backend, "grep_include", False):
                    return "no_grep_include"
            except Exception:
                return "no_grep_include"

    return None


def parse_grep_match_lines(
    stdout: str,
    *,
    root: str,
    join: Callable[[str, str], str],
    isabs: Callable[[str], bool],
    report_path: Callable[[str], str],
) -> Optional[List[Dict[str, Any]]]:
    """Parse ``PATH:LINENO:TEXT``. Unparseable → None (fallback).

    ``isabs`` comes from the backend: grep prints paths in the BACKEND's syntax,
    and a host answer mis-classifies them — CPython 3.13 stopped calling ``/x``
    absolute on Windows, so the root was joined a second time.
    """
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
        if not isabs(path):
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


def _fnmatch_escape(path: str) -> str:
    """Literal ``find -path`` pattern: backslash-escape the fnmatch metacharacters."""
    return "".join("\\" + ch if ch in "\\*?[]" else ch for ch in path)


def find_walk_head(path: str) -> List[str]:
    """``find -H PATH -mindepth 1`` — the head of every ``find`` we build.

    ``-H`` resolves a symlinked START path only (parity with ``os.walk``, which
    always follows its top); links met inside the tree stay unfollowed.
    ``-mindepth 1`` keeps the start path itself out of the prunes (search inside
    a junk dir is allowed).
    """
    return ["find", "-H", path, "-mindepth", "1"]


def find_prune_group(
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    exclude_paths: Sequence[str] = (),
) -> List[str]:
    """``( -type d -name A -o -type d -name .* -o -type d -path P )`` — the one prune
    recipe (twin of ``walk_util.pruned_dir_predicate``); ``[]`` when nothing is pruned.

    ``exclude_paths``: call-time absolute directories, pruned with the POSIX
    ``-path`` primary (escaped, so a literal path never acts as a glob).
    """
    parts: List[List[str]] = [["-type", "d", "-name", n] for n in exclude_dir_names if n]
    if exclude_nested_dot_dirs:
        parts.append(["-type", "d", "-name", ".*"])
    parts.extend(["-type", "d", "-path", _fnmatch_escape(p)] for p in exclude_paths if p)
    if not parts:
        return []
    group: List[str] = ["("]
    for i, part in enumerate(parts):
        if i:
            group.append("-o")
        group.extend(part)
    group.append(")")
    return group


def _build_find_prune_argv(
    path: str,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    print0: bool = True,
    exclude_paths: Sequence[str] = (),
) -> List[str]:
    """find PATH with basename prunes → file paths (pruned dirs are not printed:
    ``-type f`` — this listing feeds grep)."""
    argv = find_walk_head(path)
    group = find_prune_group(
        exclude_dir_names=exclude_dir_names,
        exclude_nested_dot_dirs=exclude_nested_dot_dirs,
        exclude_paths=exclude_paths,
    )
    if group:
        argv.extend(group + ["-prune", "-o"])

    argv.extend(["-type", "f"])
    if file_pattern:
        argv.extend(["-name", file_pattern.strip()])
    if print0:
        argv.append("-print0")
    else:
        argv.append("-print")
    return argv


def build_grep_argv_flags(
    *,
    grep_bin: str = "grep",
    recursive: bool = False,
    case_insensitive: bool = False,
    fixed: bool = False,
    include: Optional[str] = None,
) -> List[str]:
    """Grep flags up to (not including) ``--`` — the one source for every caller.

    Production recipes below and the ``backends.capabilities`` probes both build
    from here, so a flag can never be smoked in one and emitted in the other.

    ``-H``: always print the file name. Without it a chunk holding a single file
    yields ``LINENO:TEXT`` and parse_grep_match_lines reads the line number as a
    path (phantom files, wrong line numbers).
    """
    argv: List[str] = [grep_bin, "-H"]
    if recursive:
        argv.append("-R")
    argv.extend(["-n", "--color=never", "--no-messages", "--binary-files=without-match"])
    if case_insensitive:
        argv.append("-i")
    argv.append("-F" if fixed else "-E")
    if include:
        argv.append(f"--include={include}")
    return argv


def build_find_xargs_grep_command(
    path: str,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    grep_argv: Sequence[str],
    exclude_paths: Sequence[str] = (),
) -> str:
    """``find -print0 | xargs -0 -r grep …`` — one wire for probe and production.

    Needs GNU-ish ``find -print0`` plus ``xargs -0`` and ``xargs -r`` (without
    ``-r`` empty input may still call grep once). ``backends.capabilities``
    smokes this exact command, so a box that cannot run it never reports the
    capability gating the recipe.
    """
    find_argv = _build_find_prune_argv(
        path,
        exclude_dir_names=exclude_dir_names,
        exclude_nested_dot_dirs=exclude_nested_dot_dirs,
        file_pattern=file_pattern,
        exclude_paths=exclude_paths,
    )
    find_s = " ".join(shlex.quote(a) for a in find_argv)
    grep_s = " ".join(shlex.quote(a) for a in grep_argv)
    return f"{find_s} | xargs -0 -r {grep_s}"


def count_native_candidates(
    backend: Any,
    path: str,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    exclude_paths: Sequence[str] = (),
) -> Optional[int]:
    """Count files find would search. None on failure."""
    find_argv = _build_find_prune_argv(
        path,
        exclude_dir_names=exclude_dir_names,
        exclude_nested_dot_dirs=exclude_nested_dot_dirs,
        file_pattern=file_pattern,
        print0=False,
        exclude_paths=exclude_paths,
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


# Byte semantics for the prefilter: with LC_ALL=C ``grep -i`` folds ASCII only
# (which is exactly what prefilter_terms allows) and BOM bytes are plain bytes.
# Accepted gap: Python's re.IGNORECASE additionally folds KELVIN SIGN <-> k,
# LONG S <-> s and dotless/dotted I <-> i, so a file that relies on those code
# points could be dropped here. Code and configs never contain them, and the
# native grep path has carried the same gap since it exists; folding the terms
# for them would either disable the prefilter for most words or multiply terms.
_PREFILTER_ENV = dict(_GREP_CLEAN_ENV, LC_ALL="C")
# Seconds for the whole ``find | xargs grep -l`` pass; see native_prefilter_candidates.
PREFILTER_TIMEOUT_SEC = 60
# UTF-16 BOMs as POSIX printf octal escapes: the Python path decodes such files,
# but their UTF-16 bytes never contain the needle's UTF-8 bytes — the BOM terms
# keep them in the candidate list (over-inclusive, hence sound).
_UTF16_BOM_PRINTF = ("\\377\\376", "\\376\\377")


def prefilter_terms(needles: Needles, *, case_insensitive: bool) -> Optional[List[str]]:
    """Fixed strings whose absence proves the pattern absent — or None.

    Sound only while grep sees the bytes Python would match: control characters
    cannot travel as ``-e`` terms, and ``grep -i`` folds non-ASCII by locale, so
    case-insensitive searches keep only ASCII terms (``LC_ALL=C`` folds ASCII).
    The one accepted deviation is Python's special case folds of k/s/i to
    KELVIN SIGN / LONG S / dotless-dotted I — see ``_PREFILTER_ENV``.
    """
    if needles.required_literal is not None:
        terms = [needles.required_literal]
    elif needles.or_group:
        terms = list(needles.or_group)
    else:
        return None
    for term in terms:
        if not term or any(ord(ch) < 32 or ch == "\x7f" for ch in term):
            return None
        if case_insensitive and not term.isascii():
            return None
    return terms


def build_prefilter_command(
    path: str,
    *,
    terms: Sequence[str],
    case_insensitive: bool,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    exclude_paths: Sequence[str],
) -> str:
    """``find … -print0 | xargs -0 -r grep -l -F [-i] -e TERM… -e <BOM>…``.

    Names the candidate files inside the sandbox before a single download. No
    ``--binary-files=without-match``: a UTF-16 file (NUL bytes) must still be
    listed through the BOM terms; binaries listed the same way are dropped by
    the host-side NUL check after a head read, never a body read.
    """
    find_argv = _build_find_prune_argv(
        path,
        exclude_dir_names=exclude_dir_names,
        exclude_nested_dot_dirs=exclude_nested_dot_dirs,
        file_pattern=file_pattern,
        exclude_paths=exclude_paths,
    )
    grep_argv: List[str] = ["grep", "-l", "-F"]
    if case_insensitive:
        grep_argv.append("-i")
    grep_argv.append("--no-messages")
    for term in terms:
        grep_argv.extend(["-e", term])
    find_s = " ".join(shlex.quote(a) for a in find_argv)
    grep_s = " ".join(shlex.quote(a) for a in grep_argv)
    bom_s = " ".join(f"-e \"$(printf '{octal}')\"" for octal in _UTF16_BOM_PRINTF)
    return f"{find_s} | xargs -0 -r {grep_s} {bom_s}"


def native_prefilter_candidates(
    backend: Any,
    path: str,
    *,
    terms: Sequence[str],
    case_insensitive: bool,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    exclude_paths: Sequence[str],
) -> tuple[Optional[List[str]], Optional[str]]:
    """``(paths, None)``: absolute paths grep would open for ``terms``;
    ``(None, reason)``: the caller walks as before and tells the agent why
    (``timeout`` / ``capped`` — an incomplete listing is unsafe / ``failed``).

    One minute is the whole budget: a tree where ``grep -l`` cannot finish in
    that time is hopeless for the host-side walk anyway — fail fast instead.
    """
    cmd = build_prefilter_command(
        path,
        terms=terms,
        case_insensitive=case_insensitive,
        exclude_dir_names=exclude_dir_names,
        exclude_nested_dot_dirs=exclude_nested_dot_dirs,
        file_pattern=file_pattern,
        exclude_paths=exclude_paths,
    )
    try:
        res = backend.exec(cmd, timeout=PREFILTER_TIMEOUT_SEC, env=dict(_PREFILTER_ENV))
    except Exception as exc:
        log.debug("native prefilter failed: %s", exc)
        return None, "failed"
    if getattr(res, "timed_out", False):
        return None, "timeout"
    if getattr(res, "stdout_hit_capture_cap", False):
        return None, "capped"
    # xargs: 1 = no chunk matched, 123 = some chunk returned 1 (no match there).
    if res.return_code not in (0, 1, 123, None):
        return None, "failed"
    out: List[str] = []
    for raw in (res.stdout or "").splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        out.append(line if backend.isabs(line) else backend.join(path, line))
    return out, None


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
    allow_path: Optional[Callable[[str], bool]] = None,
    exclude_paths: Sequence[str] = (),
) -> NativeGrepResult:
    """Execute grep recipe (optionally via find prune).

    Never answers with a silent ``None``: an anomaly (exception, timeout, odd
    exit code, unparseable output) comes back as ``failure`` so the caller can
    fall back to the Python path AND name the reason to the agent.

    ``allow_path``: optional per-hit path predicate owned by the caller (this
    module knows no policy). Applied BEFORE the ``max_matches`` cut — otherwise
    rejected hits eat the budget and accepted ones are silently dropped.
    """

    def _failed(kind: str, *, hit_cap: bool = False) -> NativeGrepResult:
        return NativeGrepResult(
            matches=[], truncated=False, hit_capture_cap=hit_cap, failure=kind
        )

    names = [n for n in exclude_dir_names if n]
    need_prune = is_dir_target and (
        bool(names) or exclude_nested_dot_dirs or bool(exclude_paths)
    )

    grep_argv: List[str] = build_grep_argv_flags(
        case_insensitive=plan.case_insensitive, fixed=plan.fixed
    )
    grep_argv.extend(["--", plan.pattern])

    try:
        if need_prune:
            # Single shell pipeline with quoted argv pieces — Harbor/local POSIX.
            cmd = build_find_xargs_grep_command(
                path,
                exclude_dir_names=names,
                exclude_nested_dot_dirs=exclude_nested_dot_dirs,
                file_pattern=file_pattern,
                grep_argv=grep_argv,
                exclude_paths=exclude_paths,
            )
            res = backend.exec(cmd, timeout=120, env=dict(_GREP_CLEAN_ENV))
        elif is_dir_target:
            argv = build_grep_argv_flags(
                recursive=True,
                case_insensitive=plan.case_insensitive,
                fixed=plan.fixed,
                include=file_pattern.strip() if file_pattern else None,
            )
            argv.extend(["--", plan.pattern, path])
            res = backend.exec_argv(argv, timeout=120, env=dict(_GREP_CLEAN_ENV))
        else:
            # single file
            argv = list(grep_argv) + [path]
            res = backend.exec_argv(argv, timeout=120, env=dict(_GREP_CLEAN_ENV))
    except Exception as exc:
        log.debug("native grep failed: %s", exc)
        return _failed("native_error")

    if getattr(res, "timed_out", False):
        return _failed("native_timeout")

    rc = res.return_code
    stdout = res.stdout or ""
    stderr = res.stderr or ""
    hit_cap = bool(getattr(res, "stdout_hit_capture_cap", False))

    # xargs may return 123 if grep returns 1 on some chunks — treat carefully.
    if rc is not None and rc >= 2:
        # 123 from xargs: some invocations failed
        if rc not in (123,):
            return _failed("native_error", hit_cap=hit_cap)
        # still try parse if stdout looks clean
        if stderr.strip() and "Binary" not in stderr:
            return _failed("native_error", hit_cap=hit_cap)

    if rc == 1:
        if stderr.strip():
            return _failed("native_error", hit_cap=hit_cap)
        if stdout.strip():
            return _failed("native_error", hit_cap=hit_cap)
        return NativeGrepResult(matches=[], truncated=False, hit_capture_cap=hit_cap)

    if rc not in (0, 123, None):
        return _failed("native_error", hit_cap=hit_cap)
    if rc == 0 and not stdout.strip() and not hit_cap:
        # empty success — ok for xargs -r with zero files
        return NativeGrepResult(matches=[], truncated=False, hit_capture_cap=hit_cap)

    lines = stdout.splitlines()
    # The transport ceiling can cut the stream mid-line. The torn tail still
    # names a real file and line (a minified bundle may have only that one), so
    # it is kept and flagged below rather than dropped; if the cut fell inside
    # the file:line prefix, parsing fails and the caller falls back to Python.
    torn_tail = hit_cap and bool(lines) and not stdout.endswith("\n")
    truncated = False
    if allow_path is None:
        need = max_matches + 1
        if len(lines) > need:
            lines = lines[:need]
            truncated = True
        elif hit_cap:
            truncated = True
    elif hit_cap:
        # With a filter the pre-parse cut is unsafe: allowed hits may sit past it.
        truncated = True

    parsed = parse_grep_match_lines(
        "\n".join(lines),
        root=path,
        join=backend.join,
        isabs=backend.isabs,
        report_path=report_path,
    )
    if parsed is None:
        return _failed("native_error", hit_cap=hit_cap)
    if torn_tail and parsed:
        parsed[-1]["line_content_truncated"] = True

    if allow_path is not None:
        parsed = [m for m in parsed if allow_path(m["file"])]

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
