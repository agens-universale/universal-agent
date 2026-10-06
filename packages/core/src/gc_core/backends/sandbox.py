"""LocalFsBackend, у которого в изоляцию уходят только shell-команды модели.

Файловые тулы работают с настоящими файлами хоста и остаются под
write_guard / read_guard / protected_paths. Обёртка закрывает одно: команда
из ``execute_command`` / ``run_python_code`` / фон не пишет за пределы
``launch_root`` и, по флагу, не ходит в сеть. Linux — bwrap (список биндов —
из swarm/run-cli-in-sandbox.sh, здесь как данные: core не импортирует server),
macOS — sandbox-exec с SBPL-профилем. Нет инструмента — ошибка, не откат.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from typing import Callable, Optional, Sequence

from gc_core.backends.local import (
    LocalFsBackend,
    _arun_bounded_process,
    _sanitize_env,
)
from gc_core.backends.protocol import DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
from gc_core.config_modules.env_vars import gc_paths, project_slug

Wrapper = Callable[[Optional[str]], list[str]]

#: Системные каталоги, монтируемые read-only, если существуют (как в swarm).
_RO_SYSTEM_DIRS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt")


def _python_root() -> Optional[str]:
    """Каталог интерпретатора вне системных путей (uv-managed python) — ro-bind."""
    real = os.path.realpath(sys.executable)
    root = os.path.dirname(os.path.dirname(real))
    return None if root.startswith(_RO_SYSTEM_DIRS) else root


def _ro_bind_args(paths: Sequence[str]) -> list[str]:
    """``--ro-bind D D`` для существующих путей, без повторов."""
    seen: list[str] = []
    for d in paths:
        if d and os.path.exists(d) and d not in seen:
            seen.append(d)
    return [arg for d in seen for arg in ("--ro-bind", d, d)]


def _bwrap_ro_binds() -> list[str]:
    """``--ro-bind D D`` для системных каталогов и интерпретатора.

    ``extra_ro`` сюда НЕ входит: он биндится после rw-корня (см. wrapper) —
    поздний ``--bind root`` перекрывает ранний ro-bind пути внутри корня,
    и ``extra_ro: [<путь внутри launch_root>]`` молча становился записываемым.
    """
    return _ro_bind_args(
        [
            *(d for d in _RO_SYSTEM_DIRS if os.path.isdir(d)),
            _python_root(),
            sys.prefix,
        ]
    )


def bwrap_wrapper(
    root: str, *, network: bool, extra_ro: Sequence[str], home_to_root: bool
) -> Wrapper:
    root = os.path.realpath(root)
    home = os.path.expanduser("~")
    # Бинды собираются один раз: от вызова зависит только --chdir.
    head = ["bwrap", "--unshare-all", "--die-with-parent"]
    head += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    if network:
        head.append("--share-net")
    head += _bwrap_ro_binds()
    if home_to_root:
        head += ["--setenv", "HOME", root]
    elif os.path.isdir(home):
        # конфиги (~/.gitconfig); root ниже перекроет rw
        head += ["--ro-bind", home, home]
    head += ["--bind", root, root]
    # После корня: ro-bind внутри него иначе перекрывается rw-биндом корня.
    head += _ro_bind_args([os.path.realpath(d) for d in extra_ro])

    def wrap(cwd: Optional[str]) -> list[str]:
        return [*head, "--chdir", os.path.realpath(cwd) if cwd else root]

    return wrap


def seatbelt_wrapper(root: str, *, network: bool) -> Wrapper:
    # extra_ro на маке не нужен: (allow default) читает всё.
    writable = [
        os.path.realpath(root),
        os.path.realpath(tempfile.gettempdir()),
        "/private/tmp",
        "/dev",
    ]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write* "
        + " ".join(f'(subpath "{p}")' for p in writable)
        + ")",
    ]
    if not network:
        lines.append("(deny network*)")
    profile = "\n".join(lines)

    def wrap(_cwd: Optional[str]) -> list[str]:
        # cwd ставит сам процесс: профиль от него не зависит.
        return ["sandbox-exec", "-p", profile]

    return wrap


def _probe_bwrap(bwrap: str) -> None:
    """Fail-closed на старте: бинарь есть, а user namespaces могут быть выключены
    (CI, контейнеры) — тогда каждая команда падала бы в рантайме."""
    import subprocess

    try:
        res = subprocess.run(
            [
                bwrap,
                "--unshare-all",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            f"os_backend=sandbox: bwrap не запускается: {exc}"
        ) from exc
    if res.returncode != 0:
        raise ValueError(
            "os_backend=sandbox: bwrap не может создать namespace "
            f"(rc={res.returncode}): {res.stderr.strip()[:300]}"
        )


def build_sandbox_wrapper(root: str, section: dict) -> Wrapper:
    """Обёртка под платформу. Fail-closed: нет инструмента — ValueError."""
    s = section.get("sandbox") or {}
    network = bool(s.get("network", True))
    extra_ro = list(s.get("extra_ro") or [])
    if sys.platform == "win32":
        raise ValueError("os_backend=sandbox не поддерживается на Windows")
    if sys.platform == "darwin":
        if not shutil.which("sandbox-exec"):
            raise ValueError("os_backend=sandbox: не найден sandbox-exec")
        return seatbelt_wrapper(root, network=network)
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise ValueError("os_backend=sandbox: не найден bwrap (bubblewrap)")
    _probe_bwrap(bwrap)
    return bwrap_wrapper(
        root,
        network=network,
        extra_ro=extra_ro,
        home_to_root=s.get("workdir") == "auto",
    )


def sandbox_auto_workdir(cwd: Optional[str] = None) -> str:
    """Каталог ``<state>/sandboxes/<project_slug(cwd)>`` — launch_root песочницы.

    Один на папку запуска (тот же слаг, что у тредов и логов): повторный
    запуск и продолжение треда ``-t`` видят те же файлы, а не пустой каталог.
    Чистит его janitor ``gc_server.swarm.cleanup`` (``--core-sandboxes-dir``).
    """
    path = gc_paths()["state"] / "sandboxes" / project_slug(cwd)
    path.mkdir(parents=True, exist_ok=True)
    # Janitor судит о возрасте по mtime самого каталога, а правки внутри его
    # не обновляют — отмечаем каждый запуск, иначе живой workdir сносится.
    os.utime(path, None)
    return str(path)


class SandboxBackend(LocalFsBackend):
    """Файлы — как у LocalFsBackend; exec-семейство — через ``wrapper``."""

    def __init__(
        self,
        *,
        wrapper: Wrapper,
        root: str,
        auto_workdir: Optional[str] = None,
        exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    ) -> None:
        super().__init__(
            exec_output_capture_max_chars=exec_output_capture_max_chars
        )
        self._wrap = wrapper
        self.root = root
        self.auto_workdir = auto_workdir

    def default_cwd(self) -> Optional[str]:
        # Корень песочницы, не os.getcwd(): при launch_root ≠ cwd процесса
        # команды по умолчанию иначе шли бы вне записываемой области.
        return self.auto_workdir or self.root

    async def aexec(
        self,
        command,
        *,
        cwd=None,
        timeout=None,
        env=None,
        capture_max_chars=None,
    ):
        return await super().aexec_argv(
            [*self._wrap(cwd), "sh", "-c", command],
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
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
        return await super().aexec_argv(
            [*self._wrap(cwd), *argv],
            stdin=stdin,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
        )

    async def arun_python(
        self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None
    ):
        # Окружение чистится так же, как у LocalFsBackend.arun_python (PYTHONPATH="").
        return await _arun_bounded_process(
            [*self._wrap(cwd), sys.executable, "-u", "-"],
            shell=False,
            cwd=cwd,
            timeout=timeout,
            env=_sanitize_env(env),
            stdin_text=code,
            capture_max_chars=self._exec_capture_max_chars(capture_max_chars),
        )

    async def aspawn_background(self, command, *, cwd=None, env=None):
        # Без --die-with-parent: PDEATHSIG срабатывает на выход потока-родителя,
        # и фоновый сервис из рабочего потока хода умирал бы вместе с ходом.
        prefix = [a for a in self._wrap(cwd) if a != "--die-with-parent"]
        wrapped = " ".join(
            shlex.quote(a) for a in [*prefix, "sh", "-c", command]
        )
        return await super().aspawn_background(wrapped, cwd=cwd, env=env)
