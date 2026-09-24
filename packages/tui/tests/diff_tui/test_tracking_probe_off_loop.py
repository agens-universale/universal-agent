"""Проба перед записью файла не держит loop.

`_track_text_mutation` запускает git (`check_ignore`) и копирует файл в
`DiffStore`. Пока запись тула была синхронной, агента вёл демон-поток TUI и
loop это не касалось. После перехода на async-only агент работает НА loop —
и та же проба стала морозить его на КАЖДОЙ записи файла. Код не менялся,
изменилось, где он исполняется.

Сторожим не время, а ПОТОК: измерять заморозку бессмысленно, пока git в тесте
подменён, а вот «проба исполнилась не на loop-потоке» — это ровно тот
инвариант, который был нарушен.
"""

import threading

import pytest
from gc_tui.diff_tui import tracking as tr


class _Inner:
    """Внутренний бэкенд: только то, что зовёт обёртка."""

    def __init__(self):
        self.written: list = []

    async def write(self, path, content, encoding="utf-8"):
        self.written.append(("write", path))

    async def write_bytes(self, path, data):
        self.written.append(("write_bytes", path))

    async def append(self, path, content, encoding="utf-8"):
        self.written.append(("append", path))


@pytest.fixture
def probe_threads(monkeypatch, tmp_path):
    """Записать, на каком потоке исполнилась проба."""
    seen: list[str] = []

    def _probe(self, path):
        seen.append(threading.current_thread().name)

    monkeypatch.setattr(tr.TrackingFsBackend, "_track_text_mutation", _probe)
    return seen


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.write("/p/a.txt", "x"),
        lambda b: b.write_bytes("/p/a.txt", b"x"),
        lambda b: b.append("/p/a.txt", "x"),
    ],
    ids=["write", "write_bytes", "append"],
)
async def test_probe_runs_off_the_loop_thread(probe_threads, call):
    inner = _Inner()
    backend = tr.TrackingFsBackend(inner, ctx=None)
    loop_thread = threading.current_thread().name

    await call(backend)

    assert probe_threads, "проба не вызвана — проверяли не тот путь"
    assert loop_thread not in probe_threads, (
        f"проба исполнилась на loop-потоке: {probe_threads}"
    )


async def test_write_still_reaches_the_inner_backend(probe_threads):
    """Уход в поток не должен потерять саму запись."""
    inner = _Inner()
    backend = tr.TrackingFsBackend(inner, ctx=None)

    await backend.write("/p/a.txt", "x")

    assert inner.written == [("write", "/p/a.txt")]
