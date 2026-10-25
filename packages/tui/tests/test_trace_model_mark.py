"""Отметка смены модели в живой ленте (спека model-knob §6.4): между
ходами — обычная `✦ Model`, во время хода — тихая `✧ Model` у низа ленты,
событие «выбор применён» делает её обычной на месте или после строки
итога хода. Только живая: в тред не пишется."""

import json
from pathlib import Path

import pytest

pytest.importorskip("textual")

from gc_core.model_choice import ModelChoiceApplied
from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.palette import DIM, INK, MUTED, SUB_INK
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.services.trace_mapper import map_message
from gc_tui.widgets.model_mark import ModelMark
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.turn_line import TurnLine

_TID = "m" * 32
_REGISTRY = {
    "models": {
        "glm-flash": {},
        "glm-4.6": {},
        "giga-max": {},
        "qwen-coder": {},
    },
    "slots": {"light": "glm-flash", "default": "glm-4.6", "max": "giga-max"},
}
_CFG = {
    "agent.model": "m",
    "backend.api_mode": "chat_completions",
    "agent.auto_routing": True,
    **_REGISTRY,
}
_PENDING_NOTE = "переключится на ближайшем шаге"


class _Runner:
    """Идущий ход: применён выбор `applied`."""

    def __init__(self, applied: str):
        from gc_core.model_router import ModelRouter

        self.applied = applied
        self.running = True
        self._router = ModelRouter(
            make_settings(_REGISTRY), choice_provider=lambda: applied
        )

    def is_running(self):
        return self.running

    def applied_choice(self):
        return self.applied

    def executor_router(self):
        return self._router

    def call_level(self):
        return "default"

    def call_mode(self):
        return "manual"

    def usage_snapshot(self):
        return None

    def is_child_input_active(self):
        return False

    def produced_output(self):
        return False


def _write_thread(threads: Path) -> None:
    threads.mkdir(exist_ok=True)
    (threads / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")
    meta = {"thread_id": _TID, "messages_count": 0, "tui": {}}
    (threads / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _app(tmp_path: Path, choice: str = "default") -> TuiApp:
    threads = tmp_path / "threads"
    _write_thread(threads)
    app = TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({**_CFG, "model": choice}),
    )
    app._config_path = str(tmp_path / "config.yml")
    return app


def _activate(app: TuiApp, tmp_path: Path) -> None:
    scr = app.screen
    scr._active_task_id = _TID
    app.tui_state.active_task_id = _TID
    scr._store = FileTaskStore(
        threads_dir=str(tmp_path / "threads"), system="cli"
    )


def _busy(app: TuiApp, tmp_path: Path, applied: str = "default") -> _Runner:
    _activate(app, tmp_path)
    runner = _Runner(applied)
    app.active_runner = runner
    return runner


def _trace(scr):
    return scr.query_one("#trace")


def _marks(scr) -> list[ModelMark]:
    return list(_trace(scr).query(ModelMark))


def _call(scr, call_id: str, no: int) -> None:
    payload = {
        "id": f"f{no}",
        "type": "function_call",
        "name": "fs_read_file",
        "call_id": call_id,
        "arguments": json.dumps({"path": "README.md"}),
    }
    scr._render_event(map_message(payload, no=no, ts=""))


def _style_of(text, substr: str):
    idx = text.plain.index(substr)
    styles = [str(s.style) for s in text.spans if s.start <= idx < s.end]
    return styles[-1] if styles else str(text.style)


def _notes(mark: ModelMark) -> list[str]:
    return [b.plain for b in mark._consequences]


def _push(app: TuiApp, event: ModelChoiceApplied) -> None:
    app._trace_note_sink.push(_TID, event)


@pytest.mark.asyncio
async def test_idle_change_mounts_applied_mark_and_each_stays(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        scr._cmd_model("max")
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        first, second = _marks(scr)
        assert list(_trace(scr).children)[-2:] == [first, second]
        assert not first.pending and not second.pending
        assert first._prefix.plain == "✦ "
        assert first._content.plain == "Model  ● giga-max"
        assert second._content.plain == "Model  ● qwen-coder"
        assert _notes(first) == []
        assert _style_of(first._prefix, "✦") == SUB_INK
        assert _style_of(first._content, "Model") == SUB_INK
        assert _style_of(first._content, "giga-max") == INK


@pytest.mark.asyncio
async def test_running_change_mounts_pending_at_bottom_below_new_events(
    tmp_path,
):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        (mark,) = _marks(scr)
        assert mark.pending
        assert mark._prefix.plain == "✧ "
        assert mark._content.plain == "Model  ● giga-max"
        assert _notes(mark) == [_PENDING_NOTE]
        assert _style_of(mark._prefix, "✧") == MUTED
        assert _style_of(mark._content, "Model") == DIM
        assert _style_of(mark._content, "giga-max") == DIM
        assert _style_of(mark._consequences[0], "переключится") == MUTED
        _call(scr, "c1", 1)
        scr._mount_standalone("предупреждение", "warn")
        await pilot.pause()
        kids = list(_trace(scr).children)
        assert kids[-1] is mark
        assert any(isinstance(w, ToolCallRow) for w in kids[:-1])


@pytest.mark.asyncio
async def test_second_change_replaces_pending(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        (mark,) = _marks(scr)
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        assert _marks(scr) == [mark]
        assert mark.pending
        assert mark._content.plain == "Model  ● qwen-coder"
        assert _notes(mark) == [_PENDING_NOTE]


@pytest.mark.asyncio
async def test_reselect_working_model_removes_pending_and_hints(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        scr._cmd_model("default")
        await pilot.pause()
        assert _marks(scr) == []
        assert scr._footer_hint_text == "glm-4.6 уже работает — смены нет"
        # Между ходами: выбор, который уже действует, — тоже без отметки.
        runner.running = False
        app.active_runner = None
        scr._cmd_model("default")
        await pilot.pause()
        assert _marks(scr) == []
        assert scr._footer_hint_text == "glm-4.6 уже работает — смены нет"


@pytest.mark.asyncio
async def test_applied_event_turns_pending_into_mark_in_place(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        (mark,) = _marks(scr)
        _call(scr, "c1", 1)
        await pilot.pause()
        _push(app, ModelChoiceApplied("max", "giga-max"))
        scr._drain_trace_notes()
        await pilot.pause()
        assert _marks(scr) == [mark]
        assert not mark.pending
        assert mark._prefix.plain == "✦ "
        assert _notes(mark) == []
        assert scr._applied_frame is not None  # футер дышит вместе с лентой
        _call(scr, "c2", 2)
        await pilot.pause()
        kids = list(_trace(scr).children)
        assert kids.index(mark) < len(kids) - 1
        assert isinstance(kids[-1], ToolCallRow)


@pytest.mark.parametrize("drained_first", [True, False])
@pytest.mark.asyncio
async def test_run_end_event_places_mark_after_turn_line(
    tmp_path, drained_first
):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        (mark,) = _marks(scr)
        runner.running = False
        _push(app, ModelChoiceApplied("max", "giga-max", at_run_end=True))
        if drained_first:
            scr._drain_trace_notes()
            await pilot.pause()
            assert mark.pending  # ждёт строки итога
            assert scr._applied_frame is None
        scr._on_run_finished(runner, "run")
        await pilot.pause()
        kids = list(_trace(scr).children)
        (line,) = [w for w in kids if isinstance(w, TurnLine)]
        assert _marks(scr) == [mark]
        assert kids.index(line) < kids.index(mark)
        assert not mark.pending
        assert scr._applied_frame is not None


@pytest.mark.asyncio
async def test_marks_not_in_preview_after_reload(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        scr._cmd_model("max")
        await pilot.pause()
        assert len(_marks(scr)) == 1
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        assert _marks(scr) == []
