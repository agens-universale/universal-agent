"""Пробы шелл-делты не держат loop: git внутри них уходит в поток.

Раньше пробу, которая звала git (`check_ignore`, `git status`), держал сам
метод записи файла (`_track_text_mutation` на прежней обёртке TUI) — тогда
это было безопасно: агента вёл демон-поток, и loop это не касалось. После
перехода на async-only агент работает НА loop, а текстовые правки тулов
обёртка больше вообще не трогает (их пишет `agent_edits.recorder` прямо из
fs-тулов) — весь газ git переехал в `_atrack_shell_delta`, единственное
место, где обёртка ещё зовёт git: снимок «до» команды (`ShellDelta.begin`) и
снимок «после» (`after_and_record`). Тот же регресс подстерегает ровно здесь:
не уйди обе пробы через `asyncio.to_thread`, КАЖДЫЙ `aexec`/`aexec_argv`/
`arun_python` морозил бы loop на время git-вызовов.

Сторожим не время, а ПОТОК: измерять заморозку бессмысленно, пока git в тесте
подменён, а вот «проба исполнилась не на loop-потоке» — это ровно тот
инвариант, который был нарушен.
"""

import asyncio
import threading

import pytest

from gc_core.agent_edits import tracking as tr


class _Inner:
    """Внутренний бэкенд: только то, что зовёт обёртка."""

    async def aexec(self, command, **kw):
        return {"ok": True, "command": command}

    async def aexec_argv(self, argv, **kw):
        return {"ok": True, "argv": argv}

    async def arun_python(self, code, **kw):
        return {"ok": True, "code": code}


class _Ctx:
    config_dict: dict = {}


@pytest.fixture
def probe_threads(monkeypatch):
    """Подменить ``ShellDelta`` — записать поток, на котором позвали пробы."""
    threads: list[str] = []

    class _FakeDelta:
        @staticmethod
        def after_and_record() -> None:
            threads.append(threading.current_thread().name)

    class _FakeShellDelta:
        @staticmethod
        def begin(ctx, *, seen=None, pocket_cache=None):
            threads.append(threading.current_thread().name)
            return _FakeDelta()

    monkeypatch.setattr(tr, "ShellDelta", _FakeShellDelta)
    return threads


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.aexec("echo hi"),
        lambda b: b.aexec_argv(["echo", "hi"]),
        lambda b: b.arun_python("print(1)"),
    ],
    ids=["aexec", "aexec_argv", "arun_python"],
)
async def test_probes_run_off_the_loop_thread(probe_threads, call):
    backend = tr.TrackingFsBackend(_Inner(), _Ctx())
    loop_thread = threading.current_thread().name

    await call(backend)

    assert len(probe_threads) == 2, (
        f"begin и after_and_record обязаны отработать оба раза: {probe_threads}"
    )
    assert loop_thread not in probe_threads, (
        f"проба исполнилась на loop-потоке: {probe_threads}"
    )


async def test_command_still_reaches_the_inner_backend(probe_threads):
    """Уход проб в поток не должен потерять саму команду."""
    inner = _Inner()
    backend = tr.TrackingFsBackend(inner, _Ctx())

    result = await backend.aexec("echo hi")

    assert result == {"ok": True, "command": "echo hi"}
