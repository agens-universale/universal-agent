"""HarborBackend против НАСТОЯЩЕГО контейнера, а не подделки.

Дифференциальные oracle-тесты сверяют HarborBackend с FakeBackend, и обе
стороны там наши. Две правки этой ветки такой сверкой не проверяются вовсе:

* рабочий каталог берётся у контейнера (`aprime_default_cwd`), а не из
  константы — раньше корнем считался хост-путь раннера;
* `exists`/`lexists`/`is_symlink` ходят по внутреннему каналу и разбирают код
  возврата `test`; на битом симлинке эти три ответа расходятся, и подделка,
  повторяющая нашу же логику, расхождения бы не показала.

Плюс развилка двух каналов exec из develop: модельный `aexec` при
`exec_init="bashrc"` читает профиль оболочки, внутренний `aexec_raw` — нет.

Окружение здесь — тонкая обёртка над `docker exec`/`docker cp`: `HarborBackend`
знает у окружения ровно пять ручек, и поднимать ради теста compose-хозяйство
Harbor (environment_dir, trial_paths, task_env_config) незачем — harbor и не
объявлен зависимостью ядра. Контейнер настоящий: shell, коды возврата и
симлинки в нём не наши.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from gc_core.backends.harbor import HarborBackend

IMAGE = "python:3.12-slim"
BASHRC_MARK = "bashrc-ok"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason="docker недоступен — живой контейнер не поднять")


@dataclass
class _Result:
    stdout: str
    stderr: str
    return_code: int


class _ContainerEnv:
    """Ручки окружения, которыми пользуется HarborBackend, поверх docker CLI.

    Команду исполняем `bash -c` без `-w`: рабочий каталог берётся из WORKDIR
    образа — ровно так же, как это делает окружение Harbor.
    """

    def __init__(self, cid: str):
        self._cid = cid

    async def _run(self, args: list[str], timeout_sec: int | None = None) -> _Result:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec or 60)
        return _Result(out.decode(), err.decode(), proc.returncode or 0)

    async def exec(self, command: str, timeout_sec: int | None = None, **_: object) -> _Result:
        return await self._run(["docker", "exec", self._cid, "bash", "-c", command], timeout_sec)

    async def upload_file(self, source: Path, target: str) -> None:
        await self._run(["docker", "cp", str(source), f"{self._cid}:{target}"])

    async def download_file(self, source: str, target: Path) -> None:
        await self._run(["docker", "cp", f"{self._cid}:{source}", str(target)])

    async def is_file(self, path: str) -> bool:
        return (await self.exec(f"test -f {path}")).return_code == 0

    async def is_dir(self, path: str) -> bool:
        return (await self.exec(f"test -d {path}")).return_code == 0


@pytest.fixture
def container():
    name = f"gc-live-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-w", "/app", IMAGE, "sleep", "600"],
        capture_output=True,
        check=True,
        timeout=300,
    )
    try:
        subprocess.run(
            [
                "docker",
                "exec",
                name,
                "bash",
                "-c",
                "mkdir -p /app && echo hello > /app/file.txt"
                " && ln -s /app/file.txt /app/link && ln -s /app/nowhere /app/broken"
                f" && echo 'export GC_LIVE={BASHRC_MARK}' >> /root/.bashrc",
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture
async def backend(container):
    return HarborBackend(_ContainerEnv(container), asyncio.get_running_loop(), exec_init="bashrc")


async def test_workspace_root_is_the_container_workdir(backend):
    """Корень — WORKDIR контейнера. Раньше сюда попадал путь раннера (хоста)."""
    await backend.aprime_default_cwd()
    assert backend.default_cwd() == "/app"


async def test_broken_symlink_is_told_apart_from_a_missing_path(backend):
    """Битый симлинк: цели нет, а сам линк есть — три разных ответа."""
    assert await backend.exists("/app/file.txt") is True
    assert await backend.is_symlink("/app/file.txt") is False

    assert await backend.exists("/app/link") is True
    assert await backend.is_symlink("/app/link") is True

    assert await backend.exists("/app/broken") is False  # цель отсутствует
    assert await backend.lexists("/app/broken") is True  # но линк на месте
    assert await backend.is_symlink("/app/broken") is True

    assert await backend.exists("/app/nope") is False
    assert await backend.lexists("/app/nope") is False


async def test_read_and_write_go_through_the_container(backend):
    await backend.write("/app/written.txt", "строка\n")
    assert await backend.read("/app/written.txt") == "строка\n"
    assert await backend.is_file("/app/written.txt") is True


async def _state(backend, pid: int) -> str:
    """Состояние процесса по `/proc`, а не по `kill -0`: тот считает живым зомби."""
    res = await backend.aexec_raw(f"cut -d' ' -f3 /proc/{pid}/stat 2>/dev/null || echo gone")
    return res.stdout.strip()


async def test_a_background_command_outlives_its_exec_call(backend):
    """Фон в контейнере переживает свой exec, а pid в ответе — сама команда.

    Пока у Harbor фона не было, промпт велел модели запускать сервисы через
    `run_in_background`, а тул отказывал; `&` в обычной команде не спасал —
    трубы вызова закрывались вместе с ним.
    """
    started = await backend.aspawn_background("sleep 30")
    pid = started["pid"]

    assert await _state(backend, pid) in {"S", "R"}, "фоновый процесс не пережил свой exec"
    # Поле 6 в /proc/<pid>/stat — сессия. Своя сессия (sid == pid) — это `setsid`:
    # сигналы и конец exec-сессии фона не касаются.
    sid = await backend.aexec_raw(f"cut -d' ' -f6 /proc/{pid}/stat")
    assert sid.stdout.strip() == str(pid), f"фон остался в сессии exec: sid={sid.stdout!r}"
    cmdline = await backend.aexec_raw(f"tr '\\0' ' ' < /proc/{pid}/cmdline")
    assert "sleep 30" in cmdline.stdout, f"pid в ответе — не сама команда: {cmdline.stdout!r}"
    streams = await backend.aexec_raw(f"readlink /proc/{pid}/fd/0 /proc/{pid}/fd/1 /proc/{pid}/fd/2")
    assert streams.stdout.split() == ["/dev/null"] * 3, f"потоки фона смотрят не в /dev/null: {streams.stdout!r}"


async def test_a_background_command_gets_the_same_shell_as_the_foreground(backend):
    """Профиль оболочки, переменные и рабочий каталог — как у переднего плана."""
    await backend.aspawn_background(
        'echo "$GC_LIVE|$GC_BG|$(pwd)" > /app/bg.txt',
        cwd="/tmp",
        env={"GC_BG": "from-env"},
    )

    for _ in range(50):
        got = await backend.aexec_raw("cat /app/bg.txt 2>/dev/null")
        if got.stdout.strip():
            break
        await asyncio.sleep(0.1)
    assert got.stdout.strip() == f"{BASHRC_MARK}|from-env|/tmp", got.stdout


async def test_model_channel_reads_bashrc_and_raw_channel_does_not(backend):
    """Два канала: модельный `exec` читает профиль, внутренний — нет.

    Внутренним ходят наши же служебные команды (stat, head, test): подмешивать
    в них чужой bashrc нельзя — он печатает в stdout и портит разбор.
    """
    model = await backend.aexec("echo [$GC_LIVE]")
    assert BASHRC_MARK in model.stdout

    raw = await backend.aexec_raw("echo [$GC_LIVE]")
    assert raw.stdout.strip() == "[]"
