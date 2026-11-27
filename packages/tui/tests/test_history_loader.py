"""Загрузчик истории (спека шага 2 окна ленты, §5.7): модель строится в
фоне, одна задача за раз, отменённая — выбрасывается; поток не трогает
экран — результат уходит в главный поток через мост ``post``."""

from __future__ import annotations

import threading

from gc_tui.models import TaskCursor
from gc_tui.services import history_loader
from gc_tui.services.history_loader import HistoryLoader, history_inputs_for
from gc_tui.services.history_model import HistoryInputs

INPUTS = HistoryInputs(root="/p", injected_fields=())
CURSOR = TaskCursor(None, 0, None)


def _segments(n_records=3, n_segments=1):
    return [
        {
            "index": s,
            "compacted_by": None,
            "messages": [
                {
                    "payload": {
                        "id": f"u{s}_{i}",
                        "role": "user",
                        "content": f"реплика {s}.{i}",
                    }
                }
                for i in range(n_records)
            ],
        }
        for s in range(n_segments)
    ]


class _Store:
    """Хранилище: чтение может ждать сигнала; считает параллельные чтения."""

    def __init__(self, gates=None):
        self.gates = dict(gates or {})
        self.reads = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def read_history(self, task_id):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.reads.append(task_id)
        gate = self.gates.get(task_id)
        if gate is not None:
            gate.wait(5)
        with self._lock:
            self.active -= 1
        return _segments(), CURSOR


class _Bridge:
    """Мост в главный поток: колбэки копятся, тест исполняет их сам."""

    def __init__(self):
        self.queue = []
        self.posted = threading.Event()

    def __call__(self, fn):
        self.queue.append(fn)
        self.posted.set()

    def drain(self):
        while self.queue:
            self.queue.pop(0)()


def _raise(exc: Exception) -> None:
    raise exc


def _wait_threads():
    for t in threading.enumerate():
        if t.name == "history-loader":
            t.join(5)


def test_one_job_at_a_time():
    gate = threading.Event()
    store = _Store({"a": gate})
    bridge = _Bridge()
    loader = HistoryLoader(store, bridge)
    got = []
    loader.start(
        "a", INPUTS, on_ready=lambda r: got.append(("a", r)), on_error=_raise
    )
    gen_b = loader.start(
        "b", INPUTS, on_ready=lambda r: got.append(("b", r)), on_error=_raise
    )
    gate.set()
    _wait_threads()
    bridge.drain()
    assert store.max_active == 1
    assert [name for name, _ in got] == ["b"]
    assert gen_b == 2
    assert loader.busy is False


def test_cancel_discards_result():
    store = _Store()
    bridge = _Bridge()
    loader = HistoryLoader(store, bridge)
    got = []
    loader.start("a", INPUTS, on_ready=got.append, on_error=_raise)
    assert loader.busy is True
    loader.cancel()
    assert loader.busy is False
    _wait_threads()
    bridge.drain()
    assert got == []


def test_generation_checked_between_segments(monkeypatch):
    """Отмена посреди построения: следующая запись уже не строится."""
    bridge = _Bridge()
    loader = HistoryLoader(_Store(), bridge)
    seen = []
    real_add = history_loader.build_history.__globals__["_add_record"]

    def add(model, rec, *, seg_in_window):
        seen.append(rec["payload"]["id"])
        if len(seen) == 2:
            loader.cancel()
        real_add(model, rec, seg_in_window=seg_in_window)

    monkeypatch.setitem(
        history_loader.build_history.__globals__, "_add_record", add
    )
    got = []
    loader.start(
        "a",
        INPUTS,
        segments=_segments(n_records=3, n_segments=3),
        cursor=CURSOR,
        on_ready=got.append,
        on_error=_raise,
    )
    _wait_threads()
    bridge.drain()
    assert seen == ["u0_0", "u0_1"]
    assert got == []
    assert bridge.queue == []


def test_given_segments_are_not_read():
    store = _Store()
    bridge = _Bridge()
    loader = HistoryLoader(store, bridge)
    got = []
    segments = _segments()
    loader.start(
        "a",
        INPUTS,
        segments=segments,
        cursor=CURSOR,
        on_ready=got.append,
        on_error=_raise,
    )
    _wait_threads()
    bridge.drain()
    assert store.reads == []
    (loaded,) = got
    assert loaded.segments is segments
    assert loaded.cursor is CURSOR
    assert [e.summary for e in loaded.model.events()] == [
        "реплика 0.0",
        "реплика 0.1",
        "реплика 0.2",
    ]


def test_thread_never_touches_widgets():
    """Страж: из потока — только чтение, построение и мост; колбэк
    экрана исполняется в главном потоке."""
    bridge = _Bridge()
    loader = HistoryLoader(_Store(), bridge)
    calls = []
    loader.start(
        "a",
        INPUTS,
        withdrawn={"u0_1"},
        on_ready=lambda r: calls.append(threading.get_ident()),
        on_error=_raise,
    )
    assert bridge.posted.wait(5)
    _wait_threads()
    assert calls == []
    bridge.drain()
    assert calls == [threading.get_ident()]


def test_withdrawn_reach_the_model():
    bridge = _Bridge()
    loader = HistoryLoader(_Store(), bridge)
    got = []
    loader.start(
        "a", INPUTS, withdrawn={"u0_1"}, on_ready=got.append, on_error=_raise
    )
    _wait_threads()
    bridge.drain()
    assert got[0].model.withdrawn == {"u0_1"}


def test_failure_surfaces_on_main_thread():
    class _Broken(_Store):
        def read_history(self, task_id):
            raise OSError("диск")

    bridge = _Bridge()
    loader = HistoryLoader(_Broken(), bridge)
    errors = []
    loader.start(
        "a",
        INPUTS,
        on_ready=lambda r: None,
        on_error=lambda exc: errors.append((exc, threading.get_ident())),
    )
    _wait_threads()
    assert errors == []
    bridge.drain()
    ((exc, ident),) = errors
    assert isinstance(exc, OSError)
    assert ident == threading.get_ident()
    assert loader.busy is False


def test_history_inputs_for_takes_target_session_profile(tmp_path):
    """Входы целевой сессии: база под её профилем, корень — папка
    запуска; набор открытой сессии не трогается."""
    import json
    from pathlib import Path

    tid = "t1"
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "system_id": "cli",
                "tui": {"session_settings": {"profile": "p2"}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")

    class _Agent:
        inject_tool_fields = {"why": "", "about": ""}

    class _Loaded:
        class settings:
            agent = _Agent

    class _App:
        _threads_dir = str(tmp_path)
        _system = "cli"
        asked = []

        def session_base_settings_for(self, overrides):
            self.asked.append(overrides)
            return _Loaded

    app = _App()
    inputs = history_inputs_for(app, tid)
    assert app.asked == [{"profile": "p2"}]
    assert inputs == HistoryInputs(
        root=str(Path.cwd()), injected_fields=("about", "why")
    )
