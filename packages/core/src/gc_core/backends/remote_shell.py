"""FsBackend через чужую консоль: ``[*argv_prefix, "sh", "-c", CMD]``.

Префикс — ``docker exec -i NAME``, ``ssh … HOST --``, ``kubectl exec -i … --``:
любой CLI, который принимает команду и пробрасывает stdin/stdout. Файловые
операции — из ``PosixShellFsMixin``; байты ходят через base64 по текстовым
трубам, поэтому ни одного бинарного канала и ни одного временного файла на
хосте не нужно. Процессом владеем мы: таймаут — ``kill`` клиента (команда на
той стороне может пережить его — см. спеку §6.5).
"""

from __future__ import annotations

import base64
import os
import shlex
from pathlib import Path
from typing import Optional, Sequence

from gc_core.backends.harbor import build_harbor_shell_command
from gc_core.backends.local import _arun_bounded_process
from gc_core.backends.posix_shell_fs import PosixShellFsMixin
from gc_core.backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    WALK_OUTPUT_MAX_CHARS,
    ExecResult,
    FsBackend,
    argv_to_posix_command,
)
from gc_core.backends.walk_util import FlatWalkMixin

#: Потолок на одну файловую операцию (stat, upload, download): зависший
#: транспорт — пауза контейнера, мёртвый хост — иначе вешал бы цикл агента.
FS_OP_TIMEOUT_SEC = 300


class RemoteShellBackend(PosixShellFsMixin, FlatWalkMixin, FsBackend):
    is_sandbox = True
    supports_background = True
    default_exec_timeout = None

    def __init__(
        self,
        argv_prefix: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        exec_init: Optional[str] = None,
        exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
        walk_output_max_chars: int = WALK_OUTPUT_MAX_CHARS,
        joins_argv: bool = False,
        fs_op_timeout_sec: int = FS_OP_TIMEOUT_SEC,
    ) -> None:
        self.argv_prefix = tuple(str(a) for a in argv_prefix)
        # ssh не сохраняет argv: склеивает хвост пробелами и отдаёт строкой
        # шеллу той стороны. Таким транспортам команда уходит одним аргументом.
        self.joins_argv = joins_argv
        self.fs_op_timeout_sec = fs_op_timeout_sec
        self._cwd = cwd
        self._env = dict(env or {})
        self.exec_init = exec_init
        self.exec_output_capture_max_chars = exec_output_capture_max_chars
        self.walk_output_max_chars = walk_output_max_chars
        self._discovered_cwd: Optional[str] = None

    # --- транспорт ---
    async def _run(
        self,
        shell_command: str,
        *,
        stdin_text: Optional[str] = None,
        timeout: Optional[int] = None,
        capture_max_chars: int,
    ) -> ExecResult:
        remote = ["sh", "-c", shell_command]
        if self.joins_argv:
            remote = [shlex.join(remote)]
        return await _arun_bounded_process(
            [*self.argv_prefix, *remote],
            shell=False,
            cwd=None,
            timeout=timeout,
            env=None,
            stdin_text=stdin_text,
            capture_max_chars=capture_max_chars,
        )

    def _capture_cap(self, capture_max_chars: Optional[int]) -> int:
        return (
            self.exec_output_capture_max_chars
            if capture_max_chars is None
            else capture_max_chars
        )

    def _shell(self, command, *, cwd=None, env=None, exec_init=None) -> str:
        """``cd CWD || exit; export K=V; <init+command>`` для одного ``sh -c``.

        ``export``, а не префикс ``env K=V``: префикс достаётся только первой
        простой команде, и в ``a; b`` переменная до ``b`` не доходила.
        """
        body = build_harbor_shell_command(command, exec_init=exec_init)
        merged_env = {**self._env, **(env or {})}
        if merged_env:
            assignments = " ".join(
                f"{k}={shlex.quote(str(v))}" for k, v in merged_env.items()
            )
            body = f"export {assignments}; {body}"
        cwd = cwd or self._cwd
        if cwd:
            body = f"cd {shlex.quote(cwd)} || exit; {body}"
        return body

    # --- примитивы миксина ---
    async def _afs_exec(
        self, command: str, *, timeout: Optional[int] = None
    ) -> ExecResult:
        return await self._run(
            self._shell(command),
            timeout=timeout or self.fs_op_timeout_sec,
            capture_max_chars=self.walk_output_max_chars,
        )

    async def _aupload_bytes(self, data: bytes, path: str) -> None:
        q = shlex.quote(path)
        res = await self._run(
            f'mkdir -p "$(dirname {q})" && base64 -d > {q}',
            stdin_text=base64.b64encode(data).decode("ascii"),
            timeout=self.fs_op_timeout_sec,
            capture_max_chars=self.walk_output_max_chars,
        )
        if res.timed_out:
            raise OSError(
                f"upload timed out after {self.fs_op_timeout_sec} s: {path}"
            )
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"upload failed: {path}")

    async def _adownload_bytes(self, path: str) -> bytes:
        q = shlex.quote(path)
        # Явная проверка каталога: `base64 < dir` на macOS отдаёт rc 0 и пустой
        # вывод, и каталог читался бы как пустой файл.
        res = await self._run(
            f"if [ -d {q} ]; then echo {shlex.quote(path + ': Is a directory')} >&2; exit 1; fi; "
            f"base64 < {q}",
            timeout=self.fs_op_timeout_sec,
            capture_max_chars=self.walk_output_max_chars,
        )
        if res.timed_out:
            raise OSError(
                f"download timed out after {self.fs_op_timeout_sec} s: {path}"
            )
        if res.return_code != 0:
            err = res.stderr.strip()
            # «Файла нет» — только когда так сказала та сторона: каталог или
            # запрет на чтение иначе выглядели бы как отсутствие файла, и
            # модель шла бы его создавать.
            if "No such file" in err:
                raise FileNotFoundError(err)
            if "Permission denied" in err:
                raise PermissionError(err)
            raise OSError(err or f"download failed: {path}")
        if res.stdout_hit_capture_cap:
            raise OSError(
                f"file exceeds download ceiling ({self.walk_output_max_chars} chars): {path}"
            )
        return base64.b64decode(res.stdout or "", validate=False)

    # --- exec-семейство ---
    async def aexec(
        self,
        command,
        *,
        cwd=None,
        timeout=None,
        env=None,
        capture_max_chars=None,
    ):
        return await self._run(
            self._shell(command, cwd=cwd, env=env, exec_init=self.exec_init),
            timeout=timeout,
            capture_max_chars=self._capture_cap(capture_max_chars),
        )

    async def aexec_raw(
        self,
        command,
        *,
        cwd=None,
        timeout=None,
        env=None,
        capture_max_chars=None,
    ):
        return await self._run(
            self._shell(command, cwd=cwd, env=env),
            timeout=timeout,
            capture_max_chars=self._capture_cap(capture_max_chars),
        )

    async def aexec_argv(
        self,
        argv,
        *,
        stdin=None,
        cwd=None,
        timeout=None,
        env=None,
        capture_max_chars=None,
    ):
        return await self.aexec_raw(
            argv_to_posix_command(argv, stdin=stdin),
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
        )

    async def arun_python(
        self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None
    ):
        # Код — на stdin, не heredoc: нет коллизии с маркером и лимита длины аргумента.
        return await self._run(
            self._shell("python3 -", cwd=cwd, env=env),
            stdin_text=code,
            timeout=timeout,
            capture_max_chars=self._capture_cap(capture_max_chars),
        )

    def default_cwd(self) -> Optional[str]:
        return self._cwd or self._discovered_cwd


# --- префиксы ---
def docker_argv_prefix(section: dict) -> list[str]:
    d = section.get("docker") or {}
    argv = [d.get("binary") or "docker", "exec", "-i"]
    if d.get("user"):
        argv += ["-u", str(d["user"])]
    return argv + [str(d["name"])]


def ssh_argv_prefix(
    section: dict, *, control_dir: Optional[Path]
) -> list[str]:
    s = section.get("ssh") or {}
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-p", str(s.get("port") or 22)]
    # Мёртвый хост или оборванный канал — ошибка через секунды, а не вечное ожидание.
    argv += ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
    argv += ["-o", "ServerAliveCountMax=3"]
    if s.get("key_path"):
        argv += ["-i", os.path.expanduser(str(s["key_path"]))]
    for opt in s.get("options") or []:
        argv += ["-o", str(opt)]
    # ControlMaster: один TCP на ран. На Windows OpenSSH его не умеет.
    if (
        s.get("control_master", True)
        and control_dir is not None
        and os.name != "nt"
    ):
        control_dir.mkdir(parents=True, exist_ok=True)
        argv += [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={control_dir / 'cm-%C'}",
            "-o",
            "ControlPersist=600",
        ]
    target = f"{s['user']}@{s['host']}" if s.get("user") else str(s["host"])
    return argv + [target, "--"]
