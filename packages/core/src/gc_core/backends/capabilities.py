"""Usable-capability probes for FsBackend (find/grep).

Probes run production-shaped recipes (not ``command -v`` / ``--version`` alone):
the grep argv and the ``find | xargs | grep`` wire are built by the very helpers
production uses (``utils.native_fs_search``), so probe and production cannot
drift apart. Any step failure → False; never raise to tool callers.

Пробы КОРУТИННЫЕ: каждая запускает процессы, а в песочнице это сеть, до ~10 с
на пробу. Синхронной формы у них больше нет — она была последним, что звало
синхронный ``backend.exec``, и звала его из-под loop (через синхронные
свойства ``has_find`` / ``has_grep``).

Команды идут через ``aexec_port``, а не напрямую: у своих бэкендов это сам
бэкенд, у ЧУЖОГО синхронного — ``SyncExecAdapter``. Пробы поэтому работают на
любой сторонней реализации ``FsBackend``, не требуя от её автора корутин.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile
import uuid
from typing import TYPE_CHECKING, List, Optional, Sequence

from gc_core.backends.walk_util import WalkEntry
from gc_core.utils.native_fs_search import (
    build_find_xargs_grep_command,
    build_grep_argv_flags,
)

if TYPE_CHECKING:
    from gc_core.backends.protocol import FsBackend

_GREP_CLEAN_ENV = {
    "GREP_OPTIONS": "",
    "GREP_COLORS": "",
    "GREP_COLOR": "",
}


def _probe_tmp_name() -> str:
    return f"gc_probe_{uuid.uuid4().hex[:12]}"


def _port(backend: "FsBackend"):
    """Порт исполнения: свой бэкенд как есть, чужой синхронный — в адаптере."""
    from gc_core.backends.async_protocol import aexec_port

    return aexec_port(backend)


async def _aexec(
    backend: "FsBackend",
    command: str,
    *,
    timeout: int,
    env: Optional[dict] = None,
):
    # Сырой канал: пробам профиль образа вреден — conda-хуки пишут в stdout, а
    # `set -e` в rc подменяет код возврата. У develop так же.
    from gc_core.backends.async_protocol import arun_backend_exec_raw

    return await arun_backend_exec_raw(backend, command, timeout=timeout, env=env)


async def _arun_argv(
    backend: "FsBackend",
    argv: Sequence[str],
    *,
    stdin: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: int = 15,
):
    merged = dict(_GREP_CLEAN_ENV)
    if env:
        merged.update(env)
    return await _port(backend).aexec_argv(
        list(argv), stdin=stdin, cwd=cwd, timeout=timeout, env=merged
    )


async def _awhich_bin(backend: "FsBackend", name: str) -> Optional[str]:
    """Resolve binary via backend exec (no shell alias). Fail → None."""
    # Prefer `command -v` (POSIX); fall back to plain name.
    try:
        res = await _aexec(backend, f"command -v {shlex.quote(name)}", timeout=10)
        if res.return_code == 0:
            path = (res.stdout or "").strip().splitlines()
            if path and path[0]:
                return path[0].strip()
    except Exception:
        pass
    try:
        res = await _arun_argv(backend, [name, "--help"], timeout=5)
        # Windows find.exe --help may succeed with different semantics; callers
        # still smoke real recipes.
        if res.return_code is not None and res.return_code in (0, 1, 2):
            return name
    except Exception:
        pass
    return name  # try bare name in recipe; smoke decides


def parse_find_printf_entries(stdout: str) -> List[WalkEntry]:
    """Parse the walk wire (``%P\\t%y\\t%s\\n``; the two-column form is tolerated).

    Size is kept for regular files only; a missing or malformed third column → None.
    A symlink (``%y`` = ``l``) reports the link's own length — not the target's —
    so its size is unknown here and the consumer stats the target itself.
    """
    out: List[WalkEntry] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        if "\t" not in line:
            continue
        rel, _, tail = line.partition("\t")
        kind, _, size_text = tail.partition("\t")
        if not kind:
            continue
        # %y: f=file, d=directory, l=symlink, …
        is_dir = kind[:1] == "d"
        size: Optional[int] = None
        if kind[:1] == "f" and size_text.strip():
            try:
                size = int(size_text.strip())
            except ValueError:
                size = None
        out.append(WalkEntry(rel, is_dir, size))
    return out



async def aprobe_has_find(backend: "FsBackend") -> bool:
    """True only if the whole prune recipe runs, not just one of its flags.

    ``has_find`` exists to gate ``find -print0 | xargs -0 -r grep``
    (``utils.native_fs_search.native_grep_refusal`` is its only consumer).
    GNU ``-printf`` is Harbor walk/listdir, not this recipe — requiring it
    would fail-closed a BSD find that can still run the pipeline.
    """
    return await aprobe_find_xargs_grep(backend)


async def aprobe_has_grep(backend: "FsBackend") -> bool:
    """Smoke the production argv: -E pipe, -R tree, and the -F/-i single file."""
    grep_bin = await _awhich_bin(backend, "grep")
    if not grep_bin:
        return False
    try:
        res = await _arun_argv(
            backend,
            build_grep_argv_flags(grep_bin=grep_bin) + ["--", "b[ae]ta"],
            stdin="alpha\nbeta\n",
            timeout=10,
        )
        out = res.stdout or ""
        # -H labels stdin ``(standard input)``; accept labelled and bare rows.
        if "beta" not in out or not re.search(r"(?:^|:|\x00)\d+:", out, re.M):
            return False
    except Exception:
        return False

    tmp_remote: Optional[str] = None
    host_tmp: Optional[str] = None
    try:
        if getattr(backend, "is_sandbox", False):
            res = await _aexec(backend, "mktemp -d -t gc_probe_XXXXXX", timeout=10)
            if res.return_code != 0 or not (res.stdout or "").strip():
                tmp_remote = f"/tmp/{_probe_tmp_name()}"
                mk = await _aexec(backend, f"mkdir -p {shlex.quote(tmp_remote)}", timeout=10)
                if mk.return_code != 0:
                    return False
            else:
                tmp_remote = (res.stdout or "").strip().splitlines()[0].strip()
            fpath = f"{tmp_remote.rstrip('/')}/beta.txt"
            w = await _aexec(backend, 
                f"printf 'alpha\\nbeta\\n' > {shlex.quote(fpath)}", timeout=10
            )
            if w.return_code != 0:
                return False
            root = tmp_remote
        else:
            host_tmp = tempfile.mkdtemp(prefix="gc_probe_")
            fpath = os.path.join(host_tmp, "beta.txt")
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write("alpha\nbeta\n")
            root = host_tmp

        res = await _arun_argv(
            backend,
            build_grep_argv_flags(grep_bin=grep_bin, recursive=True)
            + ["--", "b[ae]ta", root],
            timeout=15,
        )
        if res.return_code not in (0, 1):
            return False
        text = res.stdout or ""
        if "beta" not in text:
            return False
        # Expect path<sep>line:text shape (--null: NUL after the name)
        if not re.search(r"[:\x00]\d+:", text):
            return False

        # Production takes -F for every literal/text-mode call and -i whenever
        # the call is case-insensitive: smoke both on the single-file recipe.
        res = await _arun_argv(
            backend,
            build_grep_argv_flags(grep_bin=grep_bin, fixed=True, case_insensitive=True)
            + ["--", "BETA", fpath],
            timeout=15,
        )
        if res.return_code != 0:
            return False
        text = res.stdout or ""
        return "beta" in text and bool(re.search(r"[:\x00]\d+:", text))
    except Exception:
        return False
    finally:
        try:
            if tmp_remote:
                await _aexec(backend, f"rm -rf {shlex.quote(tmp_remote)}", timeout=10)
        except Exception:
            pass
        if host_tmp:
            try:
                import shutil

                shutil.rmtree(host_tmp, ignore_errors=True)
            except Exception:
                pass


async def _amake_probe_tree(backend: "FsBackend"):
    """Create tmp with skip/hit.txt and keep/hit.txt. Returns (root, cleanup)."""
    tmp_remote: Optional[str] = None
    host_tmp: Optional[str] = None

    async def acleanup() -> None:
        try:
            if tmp_remote:
                await _aexec(backend, f"rm -rf {shlex.quote(tmp_remote)}", timeout=10)
        except Exception:
            pass
        if host_tmp:
            try:
                import shutil

                shutil.rmtree(host_tmp, ignore_errors=True)
            except Exception:
                pass

    if getattr(backend, "is_sandbox", False):
        res = await _aexec(backend, "mktemp -d -t gc_probe_XXXXXX", timeout=10)
        if res.return_code != 0 or not (res.stdout or "").strip():
            tmp_remote = f"/tmp/{_probe_tmp_name()}"
            mk = await _aexec(backend, f"mkdir -p {shlex.quote(tmp_remote)}", timeout=10)
            if mk.return_code != 0:
                return None, acleanup
        else:
            tmp_remote = (res.stdout or "").strip().splitlines()[0].strip()
        root = tmp_remote
        cmds = (
            f"mkdir -p {shlex.quote(root + '/skip')} {shlex.quote(root + '/keep')} && "
            f"printf 'secret\\n' > {shlex.quote(root + '/skip/hit.txt')} && "
            f"printf 'secret\\n' > {shlex.quote(root + '/keep/hit.txt')} && "
            f"printf 'secret\\n' > {shlex.quote(root + '/other.py')} && "
            f"printf 'secret\\n' > {shlex.quote(root + '/other.txt')}"
        )
        w = await _aexec(backend, cmds, timeout=10)
        if w.return_code != 0:
            return None, acleanup
        return root, acleanup

    host_tmp = tempfile.mkdtemp(prefix="gc_probe_")
    os.makedirs(os.path.join(host_tmp, "skip"), exist_ok=True)
    os.makedirs(os.path.join(host_tmp, "keep"), exist_ok=True)
    for rel in ("skip/hit.txt", "keep/hit.txt", "other.py", "other.txt"):
        with open(os.path.join(host_tmp, rel), "w", encoding="utf-8") as fh:
            fh.write("secret\n")
    return host_tmp, acleanup


async def aprobe_grep_exclude_dir(backend: "FsBackend") -> bool:
    grep_bin = await _awhich_bin(backend, "grep")
    if not grep_bin:
        return False
    root, acleanup = await _amake_probe_tree(backend)
    try:
        if not root:
            return False
        res = await _arun_argv(
            backend,
            build_grep_argv_flags(grep_bin=grep_bin, recursive=True)
            + ["--exclude-dir=skip", "--", "secret", root],
            timeout=15,
        )
        text = res.stdout or ""
        if "skip" in text.replace("\\", "/"):
            # path component skip should not appear
            for line in text.splitlines():
                if "/skip/" in line.replace("\\", "/") or line.replace(
                    "\\", "/"
                ).startswith("skip/"):
                    return False
                # also basename-only path forms
                if re.search(r"(^|/)skip/", line.replace("\\", "/")):
                    return False
        if "keep" not in text and "hit.txt" not in text:
            return False
        return res.return_code in (0, 1)
    except Exception:
        return False
    finally:
        await acleanup()


async def aprobe_grep_include(backend: "FsBackend") -> bool:
    grep_bin = await _awhich_bin(backend, "grep")
    if not grep_bin:
        return False
    root, acleanup = await _amake_probe_tree(backend)
    try:
        if not root:
            return False
        res = await _arun_argv(
            backend,
            build_grep_argv_flags(grep_bin=grep_bin, recursive=True, include="*.py")
            + ["--", "secret", root],
            timeout=15,
        )
        text = res.stdout or ""
        if "other.py" not in text:
            return False
        if "other.txt" in text:
            return False
        return res.return_code in (0, 1)
    except Exception:
        return False
    finally:
        await acleanup()


async def aprobe_find_xargs_grep(backend: "FsBackend") -> bool:
    """Smoke ``find -print0 | xargs -0 -r grep`` — the prune recipe, whole.

    Two steps, because ``-r`` cannot be seen from the first: a box that ignores
    it still returns hits on a non-empty tree and only misbehaves when find
    matches nothing (grep runs once on the pipe instead of not running at all).
    """
    grep_bin = await _awhich_bin(backend, "grep")
    if not grep_bin:
        return False
    grep_argv = build_grep_argv_flags(grep_bin=grep_bin) + ["--", "secret"]
    root, acleanup = await _amake_probe_tree(backend)
    try:
        if not root:
            return False
        res = await _aexec(backend, 
            build_find_xargs_grep_command(
                root,
                exclude_dir_names=["skip"],
                exclude_nested_dot_dirs=True,
                file_pattern=None,
                grep_argv=grep_argv,
                grep_env=dict(_GREP_CLEAN_ENV),
            ),
            timeout=30,
            env=dict(_GREP_CLEAN_ENV),
        )
        if res.return_code != 0:
            return False
        text = (res.stdout or "").replace("\\", "/")
        if not re.search(r"[:\x00]\d+:secret", text):
            return False
        if re.search(r"(^|/)skip/", text, re.M):
            return False

        # -r step: find matches nothing → xargs must not invoke grep at all.
        res = await _aexec(backend, 
            build_find_xargs_grep_command(
                root,
                exclude_dir_names=["skip"],
                exclude_nested_dot_dirs=True,
                file_pattern="gc_probe_no_such_file.*",
                grep_argv=grep_argv,
            ),
            timeout=30,
            env=dict(_GREP_CLEAN_ENV),
        )
        return res.return_code == 0 and not (res.stdout or "").strip()
    except Exception:
        return False
    finally:
        await acleanup()
