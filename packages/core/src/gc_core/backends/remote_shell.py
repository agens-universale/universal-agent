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
import logging
import os
import re
import shlex
import uuid
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
from gc_core.backends.runtime_probe import (
    DEFAULT_RUNTIME_PROBE_TTL_SEC,
    RuntimeProbeCache,
    RuntimeUnavailable,
)
from gc_core.backends.walk_util import FlatWalkMixin
from gc_core.user_stop import current_user_stop

#: Потолок на одну файловую операцию (stat, upload, download): зависший
#: транспорт — пауза контейнера, мёртвый хост — иначе вешал бы цикл агента.
logger = logging.getLogger(__name__)
_MISSING = object()
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Сторож на той стороне: таймаут убивает только локальный клиент
#: (`docker exec` / `ssh`), команда в контейнере или на хосте жила бы дальше.
#: Найденный инструмент запоминается. «Нет» — нет: следующий вызов спросит снова.
#: Порядок — по надёжности.
_WATCHDOG_PROBE = (
    "if timeout --version >/dev/null 2>&1; then echo timeout; "
    "elif command -v gtimeout >/dev/null 2>&1; then echo gtimeout; "
    "elif command -v setsid >/dev/null 2>&1; then echo setsid; "
    "elif command -v perl >/dev/null 2>&1; then echo perl; "
    "else echo none; fi"
)
_WATCHDOG_TOOLS = ("timeout", "gtimeout", "setsid", "perl")
#: perl есть на macOS из коробки: setpgrp делает его лидером группы, alarm
#: по истечении шлёт KILL всей группе — то же, что GNU timeout.
_PERL_WATCHDOG = (
    'setpgrp(0,0);$SIG{ALRM}=sub{kill("KILL",-getpgrp());exit(124)};'
    "alarm(shift);my $p=fork;if(!$p){exec @ARGV}waitpid($p,0);exit($?>>8)"
)
#: busybox `timeout` убивает только прямого потомка, фон переживает. Поэтому
#: тело уходит в свою сессию (`setsid`, pid = pgid) и sh-сторож бьёт по группе.
#: stdin фонового списка POSIX подменяет на /dev/null — возвращаем его через fd 3,
#: иначе `python3 -` и argv со stdin остались бы без кода.
_SETSID_WATCHDOG = (
    "exec 3<&0; setsid sh -c {body} <&3 3<&- & p=$!; "
    "(sleep {limit}; kill -TERM -- -$p; sleep 2; kill -KILL -- -$p) >/dev/null 2>&1 & w=$!; "
    "wait $p; rc=$?; kill $w >/dev/null 2>&1; exit $rc"
)
#: Запас сторожа над таймаутом клиента: `timed_out` по-прежнему решает хост.
_WATCHDOG_SLACK_SEC = 2
#: Потолок одной файловой операции миксина (зависший транспорт).
FS_OP_TIMEOUT_SEC = 300


def watchdog_wrap(
    body: str,
    timeout: Optional[int],
    tool: Optional[str],
    *,
    stop_file: Optional[str] = None,
) -> str:
    """Обернуть тело `sh -c` в сторож той стороны (GNU timeout / gtimeout / setsid / perl).

    ``stop_file`` — куда записать pid/pgid сторожа, чтобы второй канал снял
    команду тем же сигналом, не обрывая только локальный SSH.
    """
    if not timeout or not tool:
        # Сторожа нет — оборачивать нечем. Вызывающий оборвёт только ожидание.
        if not tool:
            return body
        return _stop_only_wrap(body, stop_file) if stop_file else body
    limit = int(timeout) + _WATCHDOG_SLACK_SEC
    inner = f"sh -c {shlex.quote(body)}"
    if tool in ("timeout", "gtimeout"):
        wrapped = f"{tool} {limit} {inner}"
        return _record_pid(wrapped, stop_file)
    if tool == "setsid":
        wrapped = _SETSID_WATCHDOG.format(body=shlex.quote(body), limit=limit)
        return _record_setsid_pgid(wrapped, stop_file)
    if tool == "perl":
        wrapped = f"perl -e {shlex.quote(_PERL_WATCHDOG)} {limit} {inner}"
        return _record_pid(wrapped, stop_file)
    return _stop_only_wrap(body, stop_file) if stop_file else body


def _record_pid(wrapped: str, stop_file: Optional[str]) -> str:
    """``exec`` сохраняет pid оболочки: в файле оказывается pid сторожа."""
    if not stop_file:
        return wrapped
    return f"echo $$ > {shlex.quote(stop_file)}; exec {wrapped}"


def _record_setsid_pgid(wrapped: str, stop_file: Optional[str]) -> str:
    if not stop_file:
        return wrapped
    return wrapped.replace(
        "p=$!; ", f"p=$!; echo $p > {shlex.quote(stop_file)}; ", 1
    )


def _stop_only_wrap(body: str, stop_file: Optional[str]) -> str:
    """Группа без дедлайна: стоп бьёт по ней, таймаут конфига не появляется."""
    if not stop_file:
        return body
    quoted = shlex.quote(body)
    path = shlex.quote(stop_file)
    return (
        f"exec 3<&0; setsid sh -c {quoted} <&3 3<&- & p=$!; "
        f"echo $p > {path}; wait $p"
    )


def remote_kill_command(stop_file: str) -> str:
    """Снять сторож: и сам pid (GNU timeout ловит TERM), и его группу."""
    path = shlex.quote(stop_file)
    return (
        f"pg=$(cat {path} 2>/dev/null); "
        'if [ -n "$pg" ]; then '
        'kill -TERM "$pg" 2>/dev/null; kill -TERM -- -"$pg" 2>/dev/null; '
        "sleep 0.5; "
        'kill -KILL "$pg" 2>/dev/null; kill -KILL -- -"$pg" 2>/dev/null; '
        "fi; true"
    )


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
        runtime_probe_ttl_sec: int = DEFAULT_RUNTIME_PROBE_TTL_SEC,
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
        self._runtime_probe = RuntimeProbeCache(runtime_probe_ttl_sec)
        self._discovered_cwd: Optional[str] = None

    # --- транспорт ---
    async def _aprobe(self, command: str, timeout: int) -> ExecResult:
        """``sh -c`` пробы без повторной пробы и без ``cd``.

        Потолок — трейс команды, не обход дерева: ответ пробы это одна строка.
        """
        return await self._run(
            command,
            timeout=timeout,
            capture_max_chars=self.exec_output_capture_max_chars,
            probe=False,
        )

    async def _run(
        self,
        shell_command: str,
        *,
        stdin_text: Optional[str] = None,
        timeout: Optional[int] = None,
        capture_max_chars: int,
        probe: bool = True,
        stop_file: Optional[str] = None,
    ) -> ExecResult:
        if probe:
            await self._runtime_probe.ensure(self._aprobe)
        remote = ["sh", "-c", shell_command]
        if self.joins_argv:
            remote = [shlex.join(remote)]

        async def _release(proc) -> None:
            if stop_file:
                await self._akill_remote(proc, stop_file)
                return
            from gc_core.backends.local import _astop_process

            await _astop_process(proc)

        can_kill = bool(stop_file) and bool(
            getattr(self, "_last_watchdog", None)
        )
        return await _arun_bounded_process(
            [*self.argv_prefix, *remote],
            shell=False,
            cwd=None,
            timeout=timeout,
            env=None,
            stdin_text=stdin_text,
            capture_max_chars=capture_max_chars,
            backend_name=type(self).__name__,
            stop_release=_release if stop_file else None,
            stop_unkillable=bool(stop_file) and not can_kill,
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
            bad = [k for k in merged_env if not _ENV_NAME_RE.fullmatch(str(k))]
            if bad:
                # Имя без кавычек уходит в `export` той стороны: не идентификатор —
                # это команда, а не переменная. Тулы режут раньше, это страховка.
                raise ValueError(
                    f"invalid environment variable name(s): {bad}"
                )
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
        # Переводы строк base64 снимаем явно, остальное — строго: чужой текст в
        # stdout (эхо из профиля, предупреждение sh) иначе вклеивался бы в файл.
        payload = "".join((res.stdout or "").split())
        try:
            return base64.b64decode(payload, validate=True)
        except ValueError as exc:  # binascii.Error — его подкласс
            raise OSError(
                f"download of {path} returned non-base64 data on stdout: {exc}"
            ) from exc

    # --- exec-семейство ---
    async def _awatchdog(self) -> Optional[str]:
        """Какой сторож есть на той стороне.

        Найденный инструмент запоминается. ``None`` не кэш: мёртвая оболочка,
        стоп посреди пробы и честный ``none`` спросятся снова, и следующая
        команда сможет обернуться.
        """
        cached = getattr(self, "_watchdog_tool", _MISSING)
        if cached is not _MISSING:
            return cached
        tool: Optional[str] = None
        try:
            res = await self._afs_exec(_WATCHDOG_PROBE)
            answer = (res.stdout or "").strip().splitlines()
            if (
                res.return_code == 0
                and answer
                and answer[-1] in _WATCHDOG_TOOLS
            ):
                tool = answer[-1]
        except RuntimeUnavailable:
            raise
        except Exception:
            tool = None
        if tool is None:
            if not getattr(self, "_watchdog_miss_logged", False):
                logger.warning(
                    "remote backend has no GNU timeout/gtimeout/setsid/perl: a "
                    "timed-out command keeps running on the remote side"
                )
                self._watchdog_miss_logged = True
            return None
        self._watchdog_tool = tool
        return tool

    async def _aguarded(
        self,
        body: str,
        timeout: Optional[int],
        *,
        stop_file: Optional[str] = None,
    ) -> str:
        if not timeout and not stop_file:
            self._last_watchdog = None
            return body
        tool = await self._awatchdog()
        self._last_watchdog = tool
        return watchdog_wrap(body, timeout, tool, stop_file=stop_file)

    def _stop_file(self) -> Optional[str]:
        if current_user_stop() is None:
            return None
        return f"/tmp/gc-user-stop-{uuid.uuid4().hex}"

    async def _akill_remote(self, proc, stop_file: str) -> None:
        """Второй канал дёргает сторож. Потом гасим локальное ожидание."""
        from gc_core.backends.local import _astop_process

        remote = ["sh", "-c", remote_kill_command(stop_file)]
        if self.joins_argv:
            remote = [shlex.join(remote)]
        await _arun_bounded_process(
            [*self.argv_prefix, *remote],
            shell=False,
            cwd=None,
            timeout=5,
            env=None,
            stdin_text=None,
            capture_max_chars=1000,
            backend_name=type(self).__name__,
            arm_stop=False,
        )
        await _astop_process(proc)

    async def aexec(
        self,
        command,
        *,
        cwd=None,
        timeout=None,
        env=None,
        capture_max_chars=None,
    ):
        body = self._shell(command, cwd=cwd, env=env, exec_init=self.exec_init)
        stop_file = self._stop_file()
        return await self._run(
            await self._aguarded(body, timeout, stop_file=stop_file),
            timeout=timeout,
            capture_max_chars=self._capture_cap(capture_max_chars),
            stop_file=stop_file,
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
        body = self._shell(command, cwd=cwd, env=env)
        stop_file = self._stop_file()
        return await self._run(
            await self._aguarded(body, timeout, stop_file=stop_file),
            timeout=timeout,
            capture_max_chars=self._capture_cap(capture_max_chars),
            stop_file=stop_file,
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
        body = self._shell("python3 -", cwd=cwd, env=env)
        stop_file = self._stop_file()
        return await self._run(
            await self._aguarded(body, timeout, stop_file=stop_file),
            stdin_text=code,
            timeout=timeout,
            capture_max_chars=self._capture_cap(capture_max_chars),
            stop_file=stop_file,
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
            f"ControlPath={control_dir / '%C'}",
            "-o",
            "ControlPersist=600",
        ]
    target = f"{s['user']}@{s['host']}" if s.get("user") else str(s["host"])
    return argv + [target, "--"]
