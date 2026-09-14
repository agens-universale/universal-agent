"""Native grep recipes for fs content search (capability-gated).

List / by-name enumeration is NOT here: tools use backend.walk / listdir /
iter_walk (Harbor private walk channel ~50M fail-closed; local os.walk).
Tool-exec (60k) is for raw stdout in the model trace. Native grep / prefilter
post-process the stream, so they capture at the walk ceiling (~50M).

Correctness > speed. On any anomaly callers fall back to the Python path.
"""
from __future__ import annotations

import heapq
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from gc_core.backends.async_protocol import aexec_port
from gc_core.backends.protocol import WALK_OUTPUT_MAX_CHARS
from gc_core.utils.find_in_files_scan_mode import Needles
from gc_core.utils.fs_search_walk import effective_exclude_dir_names
from gc_core.utils.pattern_native_gate import NativeGrepPlan, is_safe_grep_include

log = logging.getLogger(__name__)

_GREP_LINE_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):(?P<text>.*)$", re.ASCII)
# Tail of a ``--null`` record: the file name is already split off at NUL.
_GREP_REST_RE = re.compile(r"^(?P<line>\d+):(?P<text>.*)$", re.ASCII)
# One grep record, read straight from the buffer — no list of lines.
_RECORD_RE = re.compile(r"[^\n]+")
_GREP_CLEAN_ENV = {
    "GREP_OPTIONS": "",
    "GREP_COLORS": "",
    "GREP_COLOR": "",
}


def _split_grep_record(raw: str) -> Optional[Tuple[str, int, str]]:
    """``PATH<NUL>LINENO:TEXT`` (``--null``) or ``PATH:LINENO:TEXT``; None when neither.

    With ``--null`` a colon in a file name (``app:2026-08-31.log``) can no longer
    be mistaken for the field separator. The colon form stays accepted for a
    grep that ignores the flag, but then a colon inside the name is ambiguous
    and the record is rejected, as before.
    """
    if "\0" in raw:
        path, _, rest = raw.partition("\0")
        m = _GREP_REST_RE.match(rest)
        if not path or not m:
            return None
        return path, int(m.group("line")), m.group("text")
    m = _GREP_LINE_RE.match(raw)
    if not m or ":" in m.group("path"):
        return None
    return m.group("path"), int(m.group("line")), m.group("text")


def _grep_env(plan: NativeGrepPlan) -> Dict[str, str]:
    """Clean env; an ASCII fixed-string pattern adds byte semantics (``LC_ALL=C``).

    In a UTF-8 locale grep treats a cp1251 / latin-1 file as binary and, with
    ``--binary-files=without-match``, skips it silently — the answer then claims
    completeness. Bytes cannot miss an ASCII fixed needle (``-i`` folds ASCII
    only, which is exactly the pattern's alphabet). ERE stays in the locale even
    for ASCII patterns: under bytes ``.`` and ``[^x]`` match one BYTE, so
    ``caf.s`` would stop matching ``cafés``.
    """
    env = dict(_GREP_CLEAN_ENV)
    if plan.fixed and plan.pattern.isascii():
        env["LC_ALL"] = "C"
    return env


class _ParseFailure(Exception):
    """A record that is neither a match line nor a torn tail: the buffer is invalid."""


def _record_fields(raw: str, *, torn_tail: bool) -> Optional[Tuple[str, int, str]]:
    """``(path, line_no, text)``; None — torn last record to drop; raises on garbage."""
    rec = _split_grep_record(raw)
    if rec is not None:
        return rec
    if "Binary file" in raw or "\x1b[" in raw:
        raise _ParseFailure()
    if torn_tail:
        return None
    raise _ParseFailure()


def _winner_entries(winners: Any, report_path: Callable[[str], str], torn_tail: bool) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for _key, _i, abs_p, line_no, text, is_last in winners:
        entry: Dict[str, Any] = {
            "file": report_path(abs_p),
            "line": line_no,
            "end_line": line_no,
            "line_content": text,
            "context": text + ("\n" if not text.endswith("\n") else ""),
        }
        if torn_tail and is_last:
            entry["line_content_truncated"] = True
        matches.append(entry)
    return matches


class _RecordScan:
    """One pass over the grep buffer: yields ``(key, i, abs_path, line_no, text, is_last)``."""

    def __init__(self, stdout: str, *, hit_cap: bool, root: str, backend: Any,
                 allow_path: Optional[Callable[[str], bool]], rank_key: Optional[Callable[[str], Any]]) -> None:
        self._stdout = stdout
        self._hit_cap = hit_cap
        self._root = root
        self._backend = backend
        self._allow_path = allow_path
        self._rank_key = rank_key
        self._key_cache: Dict[str, Any] = {}
        self.total = 0
        self.files_seen: Set[str] = set()

    def _key_for(self, abs_p: str) -> Any:
        if self._rank_key is None:
            return 0
        key = self._key_cache.get(abs_p)
        if key is None:
            key = self._rank_key(abs_p)
            self._key_cache[abs_p] = key
        return key

    def records(self) -> Iterator[Tuple[Any, int, str, int, str, bool]]:
        end = len(self._stdout)
        for i, rm in enumerate(_RECORD_RE.finditer(self._stdout)):
            raw = rm.group(0).rstrip("\r")
            is_last = rm.end() == end
            rec = _record_fields(raw, torn_tail=self._hit_cap and is_last)
            if rec is None:
                return  # torn prefix: drop the tail, keep what parsed
            p, line_no, text = rec
            abs_p = p if self._backend.isabs(p) else self._backend.join(self._root, p)
            if self._allow_path is not None and not self._allow_path(abs_p):
                continue
            self.total += 1
            self.files_seen.add(abs_p)
            yield self._key_for(abs_p), i, abs_p, line_no, text, is_last


def _stream_top_k(
    stdout: str,
    *,
    hit_cap: bool,
    root: str,
    backend: Any,
    report_path: Callable[[str], str],
    allow_path: Optional[Callable[[str], bool]],
    rank_key: Optional[Callable[[str], Any]],
    top_k: Optional[int],
) -> NativeGrepResult:
    """One pass over the raw buffer; with ``rank_key`` — keep only the K best.

    The full-parse path materialises a dict per match and sorts them all — on a
    50M buffer that is ~900 MB for an answer of 100 rows. Here records are read
    straight from the buffer, the ranking key is memoised per file, a bounded
    heap keeps the K best, and only the winners become match dicts. The answer
    is identical (same key, disk order on ties); ``total_matches`` and
    ``files_seen`` feed the limit flags and the density map without the losers.

    ``rank_key=None`` — the plain path: every record in disk order (the legacy
    full parse, same brain, no second parser to drift).
    """
    torn_tail = hit_cap and not stdout.endswith("\n")
    scan = _RecordScan(stdout, hit_cap=hit_cap, root=root, backend=backend, allow_path=allow_path, rank_key=rank_key)
    try:
        if rank_key is not None and top_k is not None:
            winners = heapq.nsmallest(top_k, scan.records(), key=lambda r: (r[0], r[1]))
        else:
            winners = list(scan.records())
    except _ParseFailure:
        return NativeGrepResult(
            matches=[], truncated=False, hit_capture_cap=hit_cap, failure="native_error"
        )
    return NativeGrepResult(
        matches=_winner_entries(winners, report_path, torn_tail),
        truncated=hit_cap,
        hit_capture_cap=hit_cap,
        total_matches=scan.total,
        files_seen=scan.files_seen,
    )


def _search_capture_max_chars(backend: Any) -> int:
    """RAM cap for grep/prefilter stdout: walk ceiling, not tool-exec 60k."""
    n = getattr(backend, "walk_output_max_chars", None)
    if isinstance(n, int) and n > 0:
        return n
    return WALK_OUTPUT_MAX_CHARS


@dataclass
class NativeGrepResult:
    matches: List[Dict[str, Any]]
    truncated: bool
    hit_capture_cap: bool
    backend: str = "grep"
    # Why the recipe could not answer (``native_error`` / ``native_timeout``);
    # None on success. Callers fall back to the Python path and tell the agent.
    failure: Optional[str] = None
    # Streaming top-K only: how many records passed the filter, and every file
    # that had a hit — for the honest limit flags and the density map.
    total_matches: Optional[int] = None
    files_seen: Optional[Set[str]] = None


def _backend_flag(backend: Any, name: str) -> bool:
    try:
        return bool(getattr(backend, name, False))
    except Exception:
        return False


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
    if not _backend_flag(backend, "has_grep"):
        return "no_grep"

    names = [n for n in (exclude_dir_names or []) if n]
    need_prune = bool(names) or bool(exclude_nested_dot_dirs) or bool(call_exclude_dirs)
    if need_prune and not _backend_flag(backend, "has_find"):
        return "no_find"

    if file_pattern is not None:
        if not is_safe_grep_include(file_pattern):
            return "file_pattern_not_native"
        if not need_prune and not _backend_flag(backend, "grep_include"):
            return "no_grep_include"

    return None



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
    # Имя экранируется так же, как путь ниже: домашний предикат сравнивает строго,
    # а `-name` без экранирования читает имя как шаблон — `my[1]` вырезал бы `my1`.
    parts: List[List[str]] = [
        ["-type", "d", "-name", _fnmatch_escape(n)] for n in exclude_dir_names if n
    ]
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

    # Ссылка на файл — тоже кандидат: `find` даёт ей тип `l`, а воронка читает
    # её по цели. Без `-o -type l` список кандидатов уже того, что умеет читать тул.
    argv.extend(["(", "-type", "f", "-o", "-type", "l", ")"])
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
    yields ``LINENO:TEXT`` and the record parser reads the line number as a
    path (phantom files, wrong line numbers).

    ``--null`` puts NUL after the file name instead of the colon (GNU and BSD;
    the short ``-Z`` means *decompress* on BSD): a colon inside a file name can
    no longer break the parse. ``--devices=skip``: a symlink to a FIFO handed to
    grep as an argument blocks open() until the timeout otherwise. Both are
    always on — the probes smoke exactly what production emits, and a grep
    without them (busybox) honestly fails the probe.
    """
    argv: List[str] = [grep_bin, "-H"]
    if recursive:
        argv.append("-R")
    argv.extend(
        [
            "-n",
            "--color=never",
            "--no-messages",
            "--binary-files=without-match",
            "--null",
            "--devices=skip",
        ]
    )
    if case_insensitive:
        argv.append("-i")
    argv.append("-F" if fixed else "-E")
    if include:
        argv.append(f"--include={include}")
    return argv


def _inline_env(env: Optional[Dict[str, str]]) -> str:
    """``env K=V `` prefix for the SECOND command of a pipeline.

    Harbor forwards env by prefixing ``env K=V`` to the whole command string —
    in ``env … find … | xargs … grep …`` only find gets it. The grep half must
    carry its own assignments inline (harmless duplication on local backends).
    """
    if not env:
        return ""
    return "env " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items()) + " "


def build_find_xargs_grep_command(
    path: str,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    grep_argv: Sequence[str],
    exclude_paths: Sequence[str] = (),
    grep_env: Optional[Dict[str, str]] = None,
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
    return f"{find_s} | xargs -0 -r {_inline_env(grep_env)}{grep_s}"


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
    grep_argv: List[str] = ["grep", "-l", "--null", "-F"]
    if case_insensitive:
        grep_argv.append("-i")
    grep_argv.append("--no-messages")
    for term in terms:
        grep_argv.extend(["-e", term])
    find_s = " ".join(shlex.quote(a) for a in find_argv)
    grep_s = " ".join(shlex.quote(a) for a in grep_argv)
    bom_s = " ".join(f"-e \"$(printf '{octal}')\"" for octal in _UTF16_BOM_PRINTF)
    return f"{find_s} | xargs -0 -r {_inline_env(dict(_PREFILTER_ENV))}{grep_s} {bom_s}"


async def anative_prefilter_candidates(
    backend: Any,
    path: str,
    **kwargs: Any,
) -> tuple[Optional[List[str]], Optional[str]]:
    """Асинхронный двойник :func:`native_prefilter_candidates`.

    Тело общее: различается ровно запуск. Префильтр — ``grep -l`` по всему
    дереву с бюджетом в минуту; инлайн на loop он занял бы ровно этот бюджет,
    а поток из пула — слот на то же время. У запуска процесса нативная
    асинхронная форма есть (``aexec``), значит идём через неё.
    """
    cmd = build_prefilter_command(path, **kwargs)
    try:
        res = await aexec_port(backend).aexec(
            cmd, **_prefilter_run_kwargs(backend)
        )
    except Exception as exc:
        log.debug("native prefilter failed: %s", exc)
        return None, "failed"
    return _prefilter_result(res, backend, path)


def _prefilter_run_kwargs(backend: Any) -> Dict[str, Any]:
    """Общие параметры запуска префильтра. Команда идёт позиционно — см.
    ``_GrepRecipe.target``."""
    return {
        "timeout": PREFILTER_TIMEOUT_SEC,
        "env": dict(_PREFILTER_ENV),
        "capture_max_chars": _search_capture_max_chars(backend),
    }


def _prefilter_result(
    res: Any, backend: Any, path: str
) -> tuple[Optional[List[str]], Optional[str]]:
    """Разбор результата префильтра — общий для обеих форм."""
    if getattr(res, "timed_out", False):
        return None, "timeout"
    if getattr(res, "stdout_hit_capture_cap", False):
        return None, "capped"
    # xargs: 1 = no chunk matched, 123 = some chunk returned 1 (no match there).
    if res.return_code not in (0, 1, 123, None):
        return None, "failed"
    # `--null` просят ради имён с переводом строки (короткий `-Z` у BSD grep значит «распаковать»): GNU-grep разделяет пути NUL.
    # BSD-grep флаг принимает, но для `-l` его не соблюдает и пишет через `\n`,
    # поэтому разбор выбирается по факту наличия NUL, а не по вере в флаг.
    stdout = res.stdout or ""
    out: List[str] = []
    for raw in (stdout.split("\0") if "\0" in stdout else stdout.splitlines()):
        line = raw.strip("\r\n")
        if not line:
            continue
        out.append(line if backend.isabs(line) else backend.join(path, line))
    return out, None



@dataclass
class _GrepRecipe:
    """Как запускать grep: через shell-конвейер или напрямую argv.

    Выбор рецепта — правило, а не запуск, поэтому он один на обе формы:
    синхронную и асинхронную. Копия правил выбора неизбежно разъехалась бы,
    и нативный поиск начал бы вести себя по-разному в зависимости от того,
    каким входом его позвали.
    """

    shell: bool
    #: Первый аргумент запуска: строка команды (shell) или argv. Передаётся
    #: ПОЗИЦИОННО — чужой ``FsBackend`` вправе назвать параметр как угодно, и
    #: вызов по имени сломал бы его без всякой нужды.
    target: Any
    kwargs: Dict[str, Any]


def _grep_recipe(
    backend: Any,
    path: str,
    plan: NativeGrepPlan,
    *,
    names: Sequence[str],
    need_prune: bool,
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    exclude_paths: Sequence[str],
    grep_argv: List[str],
    env: Dict[str, str],
    is_dir_target: bool,
) -> _GrepRecipe:
    common = {
        "timeout": 120,
        "env": env,
        "capture_max_chars": _search_capture_max_chars(backend),
    }
    if need_prune:
        # Single shell pipeline with quoted argv pieces — Harbor/local POSIX.
        cmd = build_find_xargs_grep_command(
            path,
            exclude_dir_names=names,
            exclude_nested_dot_dirs=exclude_nested_dot_dirs,
            file_pattern=file_pattern,
            grep_argv=grep_argv,
            exclude_paths=exclude_paths,
            grep_env=env,
        )
        return _GrepRecipe(shell=True, target=cmd, kwargs=common)
    if is_dir_target:
        argv = build_grep_argv_flags(
            recursive=True,
            case_insensitive=plan.case_insensitive,
            fixed=plan.fixed,
            include=file_pattern.strip() if file_pattern else None,
        )
        argv.extend(["--", plan.pattern, path])
        return _GrepRecipe(shell=False, target=argv, kwargs=common)
    # single file
    return _GrepRecipe(shell=False, target=list(grep_argv) + [path], kwargs=common)


async def arun_native_grep(
    backend: Any,
    path: str,
    plan: NativeGrepPlan,
    *,
    exclude_dir_names: Sequence[str],
    exclude_nested_dot_dirs: bool,
    file_pattern: Optional[str],
    max_matches: Optional[int],
    report_path: Callable[[str], str],
    is_dir_target: bool,
    allow_path: Optional[Callable[[str], bool]] = None,
    exclude_paths: Sequence[str] = (),
    rank_key: Optional[Callable[[str], Any]] = None,
    top_k: Optional[int] = None,
) -> NativeGrepResult:
    """Асинхронный двойник :func:`run_native_grep`.

    Отличается ровно запуском: рецепт и разбор общие (``_grep_recipe`` и
    ``_grep_result``). Бюджет нативного grep — 120 секунд; инлайн на loop
    отнял бы его у приложения целиком, поток из пула — держал бы слот, а у
    запуска процесса нативная асинхронная форма есть.
    """
    names = [n for n in exclude_dir_names if n]
    need_prune = is_dir_target and (
        bool(names) or exclude_nested_dot_dirs or bool(exclude_paths)
    )
    grep_argv: List[str] = build_grep_argv_flags(
        case_insensitive=plan.case_insensitive, fixed=plan.fixed
    )
    grep_argv.extend(["--", plan.pattern])
    env = _grep_env(plan)

    recipe = _grep_recipe(
        backend,
        path,
        plan,
        names=names,
        need_prune=need_prune,
        exclude_nested_dot_dirs=exclude_nested_dot_dirs,
        file_pattern=file_pattern,
        exclude_paths=exclude_paths,
        grep_argv=grep_argv,
        env=env,
        is_dir_target=is_dir_target,
    )
    port = aexec_port(backend)
    try:
        res = (
            await port.aexec(recipe.target, **recipe.kwargs)
            if recipe.shell
            else await port.aexec_argv(recipe.target, **recipe.kwargs)
        )
    except Exception as exc:
        log.debug("native grep failed: %s", exc)
        return NativeGrepResult(
            matches=[], truncated=False, hit_capture_cap=False, failure="native_error"
        )

    return _grep_result(
        res,
        path=path,
        backend=backend,
        report_path=report_path,
        allow_path=allow_path,
        rank_key=rank_key,
        top_k=top_k,
        max_matches=max_matches,
    )


def _grep_exit_outcome(rc: Any, stdout: str, stderr: str, hit_cap: bool) -> Optional[NativeGrepResult]:
    """Итог по коду выхода grep без разбора вывода; None — вывод надо разбирать."""
    def _failed(kind: str) -> NativeGrepResult:
        return NativeGrepResult(matches=[], truncated=False, hit_capture_cap=hit_cap, failure=kind)

    empty = NativeGrepResult(matches=[], truncated=False, hit_capture_cap=hit_cap)
    if rc is not None and rc >= 2 and (rc != 123 or (stderr.strip() and "Binary" not in stderr)):
        return _failed("native_error")
    if rc == 1:
        return _failed("native_error") if (stderr.strip() or stdout.strip()) else empty
    if rc not in (0, 123, None):
        return _failed("native_error")
    if rc == 0 and not stdout.strip() and not hit_cap:
        return empty
    return None


def _grep_result(
    res: Any,
    *,
    path: str,
    backend: Any,
    report_path: Callable[[str], str],
    allow_path: Optional[Callable[[str], bool]],
    rank_key: Optional[Callable[[str], Any]],
    top_k: Optional[int],
    max_matches: Optional[int],
) -> NativeGrepResult:
    """Разбор вывода grep — общий для синхронной и асинхронной форм."""

    def _failed(kind: str, *, hit_cap: bool = False) -> NativeGrepResult:
        return NativeGrepResult(
            matches=[], truncated=False, hit_capture_cap=hit_cap, failure=kind
        )

    if getattr(res, "timed_out", False):
        return _failed("native_timeout")

    rc = res.return_code
    stdout = res.stdout or ""
    stderr = res.stderr or ""
    hit_cap = bool(getattr(res, "stdout_hit_capture_cap", False))

    # xargs may return 123 if grep returns 1 on some chunks — treat carefully.
    early = _grep_exit_outcome(rc, stdout, stderr, hit_cap)
    if early is not None:
        return early


    result = _stream_top_k(
        stdout,
        hit_cap=hit_cap,
        root=path,
        backend=backend,
        report_path=report_path,
        allow_path=allow_path,
        rank_key=rank_key,
        top_k=top_k,
    )
    if result.failure:
        return result
    if rank_key is None and max_matches is not None and len(result.matches) > max_matches:
        del result.matches[max_matches:]
        return NativeGrepResult(
            matches=result.matches,
            truncated=True,
            hit_capture_cap=hit_cap,
            total_matches=result.total_matches,
            files_seen=result.files_seen,
        )
    return result


def hard_exclude_names_for_grep(extra: Optional[Sequence[str]] = None) -> List[str]:
    """Basenames for prune/--exclude-dir (hard floor ∪ config extras)."""
    return sorted(effective_exclude_dir_names(extra))
