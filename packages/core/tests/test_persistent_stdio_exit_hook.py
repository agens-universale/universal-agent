"""Выход процесса не должен вставать в очередь за локом сессии.

Правка M16 повесила ``close_everything`` на ``atexit``. Он зовёт
``session.close()``, а тот брал лок БЛОКИРУЮЩИМ ``with self._lock`` — то есть
ровно ту заморозку, от которой асинхронный близнец уже ушёл (H1), только
теперь на выходе из процесса и без таймаута.

Сценарий: abandonable-поток пережил отмену вызывающего и сидит в ``readline``
под локом. До появления хука процесс просто выходил; с хуком он ждал бы
держателя до ``request_timeout_sec`` — по умолчанию 600 секунд.

Второе свойство: при занятом локе ПРОЛОГ (обмен close-тулом) пропускается.
Держатель сидит на том же ридере, ответ close-тула ушёл бы в его очередь, и мы
прождали бы десять секунд впустую, чтобы всё равно перейти к терминации.
"""
import threading
import time

import pytest
from gc_core.tools.connect_mcp import persistent_stdio as ps


class _Pipe:
    """Труба-пустышка: закрывается молча, писать в неё можно."""

    def __init__(self) -> None:
        self.closed = False

    def write(self, _data):
        return None

    def flush(self):
        return None

    def close(self):
        self.closed = True

    def readline(self):
        return ""


class _FakeProc:
    """Процесс-пустышка: терминация мгновенна, обмен фиксируется.

    Трубы настоящие по форме: без них закрытие падает на ``stdin`` РАНЬШЕ, чем
    доходит до замера, и страж краснел бы по постороннему поводу.
    """

    def __init__(self) -> None:
        self.pid = -1
        self.killed = False
        self.stdin = _Pipe()
        self.stdout = _Pipe()
        self.stderr = _Pipe()

    def poll(self):
        return 0 if self.killed else None

    def wait(self, timeout=None):
        self.killed = True
        return 0

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True


def _session(monkeypatch, *, close_tool: str | None = None):
    sess = ps.PersistentStdioSession.__new__(ps.PersistentStdioSession)
    sess._lock = threading.Lock()
    sess._proc = _FakeProc()
    sess._server_cfg = (
        {"persistent_session_close_tool": close_tool} if close_tool else {}
    )
    sess._server_name = "probe"

    # Двойник повторяет ФОРМУ прода: терминация теперь только корутинная,
    # а `close()` ждёт её своим `asyncio.run`. Синхронная подмена вернула бы
    # не-awaitable, и `asyncio.run` упал бы на ней, а не на нашей проверке.
    async def _kill(proc, **_kw):
        proc.kill()

    monkeypatch.setattr(ps, "_aterminate_process_tree", _kill)
    return sess


def test_close_does_not_wait_for_a_held_lock(monkeypatch):
    """Главное свойство: close() возвращается сразу, лок держит чужой поток."""
    sess = _session(monkeypatch)
    holder_in = threading.Event()
    release = threading.Event()

    def _holder():
        with sess._lock:
            holder_in.set()
            release.wait(10)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert holder_in.wait(5), "проверка пустая: держатель не взял лок"

    started = time.monotonic()
    sess.close()
    elapsed = time.monotonic() - started
    release.set()
    t.join(5)

    assert elapsed < 1.0, (
        f"close() ждал лок {elapsed:.2f} с — atexit подвесит выход процесса"
    )
    assert sess._proc is None, "процесс не снят с сессии"


def test_prologue_is_skipped_when_the_lock_is_busy(monkeypatch):
    """Занят лок — close-тул не пишем: ответ ушёл бы держателю ридера."""
    sess = _session(monkeypatch, close_tool="browser_close_all")
    wrote: list = []
    monkeypatch.setattr(
        ps.PersistentStdioSession, "_write_close_tool",
        lambda self, proc, tool: wrote.append(tool) or "req-1",
    )
    monkeypatch.setattr(
        ps.PersistentStdioSession, "_read_matching_on",
        lambda self, proc, req_id, timeout: pytest.fail("читали чужой ридер"),
    )
    monkeypatch.setattr(
        ps.PersistentStdioSession, "_close_prologue_tail", lambda self, proc: None
    )

    holder_in = threading.Event()
    release = threading.Event()

    def _holder():
        with sess._lock:
            holder_in.set()
            release.wait(10)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert holder_in.wait(5)

    sess.close()
    release.set()
    t.join(5)

    assert wrote == [], f"close-тул отправлен при занятом локе: {wrote}"


def test_prologue_still_runs_when_the_lock_is_free(monkeypatch):
    """А на свободном локе пролог обязан работать — иначе браузер не гасится.

    Это и есть цена вопроса: close-тул (``browser_close_all``) — единственный
    штатный способ погасить detached-потомков сервера.
    """
    sess = _session(monkeypatch, close_tool="browser_close_all")
    wrote: list = []
    monkeypatch.setattr(
        ps.PersistentStdioSession, "_write_close_tool",
        lambda self, proc, tool: wrote.append(tool) or "req-1",
    )
    monkeypatch.setattr(
        ps.PersistentStdioSession, "_read_matching_on",
        lambda self, proc, req_id, timeout: {"id": req_id},
    )
    monkeypatch.setattr(
        ps.PersistentStdioSession, "_close_prologue_tail", lambda self, proc: None
    )

    sess.close()

    assert wrote == ["browser_close_all"], (
        "на свободном локе close-тул не отправлен — потомки сервера переживут агента"
    )
