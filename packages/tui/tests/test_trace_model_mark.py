"""Отметка смены модели в живой ленте (спека model-knob §6.4, раунд 3
смока): между ходами — черта во всю ширину `──── знак ────`, во время
хода — знак с тусклым именем и следствием у низа ленты; событие «выбор
применён» делает её чертой на месте или после строки итога хода.
Отметки подряд не копятся: новая заменяет предыдущую. Смену между
ходами TUI пишет в тред через хранилище (запись `model_choice`), смену в
ходе — ядро; восстановление их рисует (`test_model_choice_readers`)."""

import json
from pathlib import Path

import pytest

pytest.importorskip("textual")

from gc_core.model_choice import ModelChoiceApplied
from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.palette import DEEP_DIM, DIM, MUTED, RUN_COLOR, TRACK
from gc_tui.services.model_sign import FREE_COLOR
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
_PENDING_NOTE = "модель переключится на ближайшем шаге"


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


async def _assert_rule(pilot, mark: ModelMark, label: str) -> None:
    """Черта во всю ширину строки: `──── label ────`, полосы TRACK,
    подпись DEEP_DIM, без колонки глифа и следствий. Пауза — пересборка
    под ширину после раскладки."""
    await pilot.pause()
    plain = mark._content.plain
    assert str(mark._prefix) == ""
    assert len(plain) == mark.size.width > len(label) + 4
    assert plain.strip("─") == f" {label} "
    assert plain.startswith("──") and plain.endswith("──")
    left = len(plain) - len(plain.lstrip("─"))
    right = len(plain) - len(plain.rstrip("─"))
    assert abs(left - right) <= 1
    assert _style_of(mark._content, "─") == TRACK
    name = label.split(" ", 1)[1]
    assert _style_of(mark._content, name) == DEEP_DIM
    assert _notes(mark) == []


@pytest.mark.asyncio
async def test_idle_change_mounts_rule_mark(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        (mark,) = _marks(scr)
        assert list(_trace(scr).children)[-1] is mark
        assert not mark.pending
        await _assert_rule(pilot, mark, "● giga-max")
        assert _style_of(mark._content, "●") == RUN_COLOR


@pytest.mark.asyncio
async def test_idle_consecutive_changes_collapse_into_one_mark(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        (mark,) = _marks(scr)
        assert list(_trace(scr).children)[-1] is mark
        await _assert_rule(pilot, mark, "● qwen-coder")
        assert _style_of(mark._content, "●") == FREE_COLOR


@pytest.mark.asyncio
async def test_idle_three_changes_leave_one_rule(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        for choice in ("max", "qwen-coder", "light"):
            scr._cmd_model(choice)
        await pilot.pause()
        (mark,) = _marks(scr)
        await _assert_rule(pilot, mark, "● glm-flash")


@pytest.mark.asyncio
async def test_idle_return_to_previous_model_leaves_no_rule(tmp_path):
    """max → qwen-coder → max между ходами: модель до первой черты была
    max, смены нет — черты нет."""
    app = _app(tmp_path, choice="max")
    async with app.run_test() as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        scr._cmd_model("max")
        await pilot.pause()
        assert _marks(scr) == []


@pytest.mark.asyncio
async def test_idle_changes_with_event_between_keep_both(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        scr._cmd_model("max")
        scr._mount_standalone("предупреждение", "warn")
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        first, second = _marks(scr)
        await _assert_rule(pilot, first, "● giga-max")
        await _assert_rule(pilot, second, "● qwen-coder")


@pytest.mark.asyncio
async def test_rule_follows_width_on_resize(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        (mark,) = _marks(scr)
        wide = len(mark._content.plain)
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        assert len(mark._content.plain) == mark.size.width < wide
        await _assert_rule(pilot, mark, "● giga-max")


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
        assert mark._prefix.plain == "● "
        assert _style_of(mark._prefix, "●") == RUN_COLOR
        assert mark._content.plain == "giga-max"
        assert _style_of(mark._content, "giga-max") == DIM
        assert _notes(mark) == [_PENDING_NOTE]
        assert _style_of(mark._consequences[0], "модель") == MUTED
        assert "Model" not in mark._content.plain
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
        assert mark._prefix.plain == "● "
        assert _style_of(mark._prefix, "●") == FREE_COLOR
        assert mark._content.plain == "qwen-coder"
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
        await _assert_rule(pilot, mark, "● giga-max")
        assert not hasattr(scr, "_applied_frame")  # без анимации
        _call(scr, "c2", 2)
        await pilot.pause()
        kids = list(_trace(scr).children)
        assert kids.index(mark) < len(kids) - 1
        assert isinstance(kids[-1], ToolCallRow)


@pytest.mark.asyncio
async def test_applied_mark_closes_group_above_it(tmp_path):
    """Ожидание группу не рвёт, черта — рвёт, как после восстановления:
    чтение после применённой смены открывает свою группу."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _busy(app, tmp_path)
        _call(scr, "c1", 1)
        scr._cmd_model("max")
        await pilot.pause()
        _call(scr, "c2", 2)
        await pilot.pause()
        model = scr._top_blocks().model
        assert model.block_of("c2") == model.block_of("c1")
        _push(app, ModelChoiceApplied("max", "giga-max"))
        scr._drain_trace_notes()
        await pilot.pause()
        _call(scr, "c3", 3)
        await pilot.pause()
        assert model.block_of("c3") != model.block_of("c2")


async def _apply(pilot, scr, app, choice: str, alias: str) -> None:
    _push(app, ModelChoiceApplied(choice, alias))
    scr._drain_trace_notes()
    await pilot.pause()


@pytest.mark.asyncio
async def test_running_consecutive_applied_marks_collapse(tmp_path):
    """Во время хода: применённая отметка, за ней сразу ещё одна — без
    событий между ними — остаётся одна, последняя."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        _call(scr, "c1", 1)
        scr._cmd_model("max")
        await pilot.pause()
        await _apply(pilot, scr, app, "max", "giga-max")
        runner.applied = "max"
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        first, pending = _marks(scr)
        assert pending.pending and not first.pending
        await _apply(pilot, scr, app, "qwen-coder", "qwen-coder")
        (mark,) = _marks(scr)
        assert mark is pending
        await _assert_rule(pilot, mark, "● qwen-coder")
        kids = list(_trace(scr).children)
        assert isinstance(kids[kids.index(mark) - 1], ToolCallRow)


@pytest.mark.asyncio
async def test_running_return_to_previous_model_leaves_no_rule(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        _call(scr, "c1", 1)
        scr._cmd_model("max")
        await pilot.pause()
        await _apply(pilot, scr, app, "max", "giga-max")
        runner.applied = "max"
        scr._cmd_model("default")
        await pilot.pause()
        await _apply(pilot, scr, app, "default", "glm-4.6")
        assert _marks(scr) == []


@pytest.mark.asyncio
async def test_running_applied_marks_with_event_between_keep_both(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        await _apply(pilot, scr, app, "max", "giga-max")
        runner.applied = "max"
        _call(scr, "c1", 1)
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        await _apply(pilot, scr, app, "qwen-coder", "qwen-coder")
        first, second = _marks(scr)
        await _assert_rule(pilot, first, "● giga-max")
        await _assert_rule(pilot, second, "● qwen-coder")


async def _ticks(pilot, n: int = 4) -> None:
    for _ in range(n):  # тик опроса — 250 мс
        await pilot.pause(0.25)


@pytest.mark.asyncio
async def test_choice_seeded_at_run_start_applies_pending_in_place(tmp_path):
    """Окно старта рана: выбор сделан до засева применённого — ран уже
    идёт им, события не будет. Тихая отметка — обычной на месте, пока ран
    идёт; запоздалое событие того же выбора вторую отметку не ставит."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        scr._cmd_model("max")
        await _ticks(pilot, 1)
        (mark,) = _marks(scr)
        assert mark.pending
        runner.applied = "max"
        await _ticks(pilot, 2)
        assert runner.is_running()
        assert _marks(scr) == [mark]
        assert not mark.pending
        await _assert_rule(pilot, mark, "● giga-max")
        _push(app, ModelChoiceApplied("max", "giga-max"))
        _call(scr, "c1", 1)
        await _ticks(pilot, 1)
        assert _marks(scr) == [mark]
        kids = list(_trace(scr).children)
        assert kids.index(mark) < len(kids) - 1
        runner.running = False
        app.active_runner = None


def _after_turn_line(scr, mark: ModelMark) -> bool:
    kids = list(_trace(scr).children)
    (line,) = [w for w in kids if isinstance(w, TurnLine)]
    return kids.index(line) < kids.index(mark)


@pytest.mark.parametrize("with_event", [True, False])
@pytest.mark.asyncio
async def test_run_end_event_places_mark_after_turn_line(tmp_path, with_event):
    """Ран кончился: тихая отметка — обычной после строки итога. Без
    события тоже: выбор, сделанный до засева применённого на старте,
    события не даёт."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        scr._cmd_model("max")
        await _ticks(pilot, 2)
        (mark,) = _marks(scr)
        assert mark.pending
        if with_event:
            _push(app, ModelChoiceApplied("max", "giga-max", at_run_end=True))
        runner.running = False
        await _ticks(pilot)
        assert _marks(scr) == [mark]
        assert not mark.pending
        await _assert_rule(pilot, mark, "● giga-max")
        assert _after_turn_line(scr, mark)


@pytest.mark.asyncio
async def test_stop_applies_mark_after_stop_line_once_thread_exits(tmp_path):
    """Стоп: событие конца рана приходит, когда поток ещё дорабатывает;
    отметка ждёт его выхода и встаёт после строки итога стопа."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        scr._cmd_model("max")
        await _ticks(pilot, 2)
        (mark,) = _marks(scr)
        app.last_stop_requested = True
        scr._stopping_runner = runner
        app.active_runner = None
        await _ticks(pilot)
        _push(app, ModelChoiceApplied("max", "giga-max", at_run_end=True))
        await _ticks(pilot)
        assert mark.pending  # поток стопа жив
        runner.running = False
        await _ticks(pilot)
        assert not mark.pending
        assert _after_turn_line(scr, mark)


@pytest.mark.asyncio
async def test_run_end_does_not_leak_into_another_session(tmp_path):
    other = "n" * 32
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        runner = _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        _push(app, ModelChoiceApplied("max", "giga-max", at_run_end=True))
        scr._drain_trace_notes()
        threads = tmp_path / "threads"
        (threads / f"data_cli_{other}.jsonl").write_text("", encoding="utf-8")
        (threads / f"meta_cli_{other}.json").write_text(
            json.dumps({"thread_id": other, "tui": {}}), encoding="utf-8"
        )
        scr.attach_active_task(other)
        app.tui_state.active_task_id = other
        _push(app, ModelChoiceApplied("max", "giga-max", at_run_end=True))
        runner.running = False
        await _ticks(pilot)
        assert _marks(scr) == []


@pytest.mark.asyncio
async def test_plan_panel_goes_above_turn_line_past_applied_mark(tmp_path):
    from gc_tui.widgets.plan_panel import PlanPanel

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = app.screen
        _activate(app, tmp_path)
        scr._show_turn_outcome("completed", True, None)
        scr._cmd_model("max")
        await pilot.pause()
        scr._ensure_plan_panel_last({"text": "шаг 1"}, above_fresh_line=True)
        await pilot.pause()
        kids = list(_trace(scr).children)
        (panel,) = [w for w in kids if isinstance(w, PlanPanel)]
        (line,) = [w for w in kids if isinstance(w, TurnLine)]
        (mark,) = _marks(scr)
        assert kids.index(panel) < kids.index(line) < kids.index(mark)


def _write_messages(threads: Path, msgs: list[dict]) -> None:
    from gc_tui.services.stores.file_store import FileTaskStore

    FileTaskStore(str(threads), system="cli").append_messages(_TID, msgs)


def _write_dialog(threads: Path) -> None:
    """Тред с одним ходом: две записи сообщений."""
    _write_messages(
        threads,
        [
            {"id": "u1", "role": "user", "content": "вопрос"},
            {"id": "a1", "role": "assistant", "content": "ответ"},
        ],
    )


def _choice_rows(tmp_path: Path) -> list[dict]:
    """Записи потока смен модели треда (``after``, ``model``)."""
    from gc_testkit.thread_writes import model_choice_records

    return model_choice_records(str(tmp_path / "threads"), "cli", _TID)


@pytest.mark.asyncio
async def test_idle_change_is_written_and_restored(tmp_path):
    from gc_core.persistence.model_choice_record import ModelChoiceRecord

    app = _app(tmp_path)
    _write_dialog(tmp_path / "threads")
    async with app.run_test() as pilot:
        scr = app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        scr._cmd_model("max")
        await pilot.pause()
        (record,) = _choice_rows(tmp_path)
        assert record == {
            "after": "a1",
            "model": ModelChoiceRecord(
                "max", "giga-max", "glm-4.6"
            ).to_payload(),
        }
        assert len(_marks(scr)) == 1
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        (mark,) = _marks(scr)
        await _assert_rule(pilot, mark, "● giga-max")


@pytest.mark.asyncio
async def test_running_change_is_not_written_by_tui(tmp_path):
    """Смену в ходе пишет ядро, когда она применена, — не TUI."""
    app = _app(tmp_path)
    _write_dialog(tmp_path / "threads")
    async with app.run_test() as pilot:
        scr = app.screen
        _busy(app, tmp_path)
        scr._cmd_model("max")
        await pilot.pause()
        assert _choice_rows(tmp_path) == []


@pytest.mark.asyncio
async def test_change_before_first_message_is_not_written(tmp_path):
    """Треда ещё нет (ни одного сообщения) — смена не пишется и после
    перечитывания черты нет."""
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


# ---- отметка следует за моделью следующего хода (смок, п.15) ------------
#
# Смена между ходами, меняющая модель следующего хода (профиль, «модель по
# умолчанию» в Настройках, перечитанный конфиг), ставит черту так же, как
# `/model`: склейка с соседней, возврат к прежней модели — без черты, запись
# в тред. Сравнивается модель, а не выбор. Отметок в ленте нет — сверка с
# моделью, которой пошёл бы ход до смены. Во время хода лента не
# трогается: сверка — после конца хода, с моделью хода.

_CONFIG = {
    "backend": {
        "api_mode": "chat_completions",
        "completions_url": "http://base/v1/chat/completions",
    },
    "models": {"glm-flash": {}, "glm-4.6": {}, "qwen-coder": {}},
    "slots": {"light": "glm-flash", "default": "glm-4.6"},
    "model": "default",
}


def _write_config(tmp_path: Path, **changes) -> Path:
    import yaml

    body = {
        **_CONFIG,
        "paths": {"threads_data_path": str(tmp_path / "threads")},
        **changes,
    }
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _write_profile(tmp_path: Path, name: str, body: dict) -> None:
    import yaml

    path = tmp_path / "profiles" / f"{name}.yml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


def _config_app(tmp_path: Path) -> TuiApp:
    """Окно на настоящем config.yml и профилях рядом с ним; тред с одним
    ходом."""
    from gc_core.config import load_settings

    threads = tmp_path / "threads"
    _write_thread(threads)
    _write_dialog(threads)
    cfg = _write_config(tmp_path)
    _write_profile(tmp_path, "work", {"slots": {"light": "qwen-coder"}})
    _write_profile(tmp_path, "keep", {"slots": {"default": "qwen-coder"}})
    app = TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=load_settings(str(cfg)),
    )
    app.set_config_path(str(cfg))
    return app


def _write_history_with_rule(threads: Path) -> None:
    """Ход, черта «glm-4.6» (смена в ходе, без сессионного выбора), ещё
    ход: последняя отметка ленты — glm-4.6, за ней сообщения."""
    from gc_core.persistence.model_choice_record import ModelChoiceRecord
    from gc_testkit.thread_writes import model_choice_record

    record = ModelChoiceRecord("default", "glm-4.6", "glm-flash")
    model_choice_record(str(threads), "cli", _TID, record)
    _write_messages(
        threads,
        [
            {"id": "u2", "role": "user", "content": "ещё вопрос"},
            {"id": "a2", "role": "assistant", "content": "ещё ответ"},
        ],
    )


def _choice_records(tmp_path: Path) -> list:
    from gc_core.persistence.model_choice_record import (
        MODEL_CHOICE_TYPE,
        parse_model_choice,
    )

    return [
        parse_model_choice({"type": MODEL_CHOICE_TYPE, **r})
        for r in _choice_rows(tmp_path)
    ]


async def _attached(pilot, app: TuiApp) -> object:
    scr = app.screen
    scr.attach_active_task(_TID, is_resume=True)
    app.tui_state.active_task_id = _TID
    await pilot.pause()
    return scr


@pytest.mark.asyncio
async def test_profile_switch_changing_next_model_replaces_rule(tmp_path):
    """Смок: /model light (черта glm-flash), профиль, где light — другая
    модель: черта одна — новой модели, в треде запись смены."""
    from gc_core.persistence.model_choice_record import ModelChoiceRecord

    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        scr._cmd_model("light")
        await pilot.pause()
        app.switch_session_profile("work")
        await pilot.pause()
        (mark,) = _marks(scr)
        await _assert_rule(pilot, mark, "● qwen-coder")
        assert list(_trace(scr).children)[-1] is mark
        assert _choice_records(tmp_path)[-1] == ModelChoiceRecord(
            "light", "qwen-coder", "glm-flash"
        )
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        (restored,) = _marks(scr)
        await _assert_rule(pilot, restored, "● qwen-coder")


@pytest.mark.asyncio
async def test_profile_switch_keeping_next_model_adds_no_rule(tmp_path):
    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        scr._cmd_model("light")
        await pilot.pause()
        (mark,) = _marks(scr)
        written = _choice_records(tmp_path)
        app.switch_session_profile("keep")
        await pilot.pause()
        assert _marks(scr) == [mark]
        await _assert_rule(pilot, mark, "● glm-flash")
        assert _choice_records(tmp_path) == written


@pytest.mark.asyncio
async def test_config_change_back_to_model_before_rule_leaves_none(tmp_path):
    """/model light (черта glm-flash вместо glm-4.6), перечитанный конфиг
    делает light снова glm-4.6: смены модели нет — черты нет. Выбор при
    этом прежний: сравнивается модель."""
    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        scr._cmd_model("light")
        await pilot.pause()
        _write_config(tmp_path, slots={"light": "glm-4.6"})
        app.reload_config()
        await pilot.pause()
        assert _marks(scr) == []


@pytest.mark.asyncio
async def test_settings_model_default_write_adds_rule(tmp_path):
    from gc_core.persistence.model_choice_record import ModelChoiceRecord
    from gc_tui.screens.settings import SettingsScreen

    app = _config_app(tmp_path)
    _write_history_with_rule(tmp_path / "threads")
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        (restored,) = _marks(scr)
        settings = SettingsScreen(
            settings=app.base_settings(),
            config_path=app._config_path,
            on_apply=app.reload_config,
        )
        app.push_screen(settings)
        await pilot.pause()
        settings._write_model_default("light")
        await pilot.pause()
        first, mark = _marks(scr)
        assert first is restored
        await _assert_rule(pilot, mark, "● glm-flash")
        assert list(_trace(scr).children)[-1] is mark
        assert _choice_records(tmp_path)[-1] == ModelChoiceRecord(
            "light", "glm-flash", "glm-4.6"
        )


@pytest.mark.asyncio
async def test_config_reload_changing_next_model_adds_rule(tmp_path):
    app = _config_app(tmp_path)
    _write_history_with_rule(tmp_path / "threads")
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        _write_config(tmp_path, slots={"default": "qwen-coder"})
        app.reload_config_if_changed()
        await pilot.pause()
        _, mark = _marks(scr)
        await _assert_rule(pilot, mark, "● qwen-coder")


@pytest.mark.asyncio
async def test_empty_trace_profile_switch_changing_model_adds_rule(tmp_path):
    """Ход без отметок шёл glm-4.6; профиль, где default — qwen-coder:
    черта, как у `/model`, с той же записью."""
    from gc_core.persistence.model_choice_record import ModelChoiceRecord

    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        assert _marks(scr) == []
        app.switch_session_profile("keep")
        await pilot.pause()
        (mark,) = _marks(scr)
        await _assert_rule(pilot, mark, "● qwen-coder")
        assert _choice_records(tmp_path) == [
            ModelChoiceRecord("default", "qwen-coder", "glm-4.6")
        ]


@pytest.mark.asyncio
async def test_empty_trace_profile_switch_keeping_model_adds_none(tmp_path):
    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        app.switch_session_profile("work")
        await pilot.pause()
        assert _marks(scr) == []
        assert _choice_records(tmp_path) == []


async def _finish_run(pilot, app: TuiApp) -> None:
    """Тик застаёт ход бегущим, затем ход кончается."""
    await _ticks(pilot, 1)
    app.active_runner.running = False
    await _ticks(pilot)
    app.active_runner = None
    await pilot.pause()


@pytest.mark.asyncio
async def test_config_change_during_run_is_followed_after_run(tmp_path):
    """Во время хода лента не трогается; ход кончился — черта модели
    следующего хода и запись."""
    from gc_core.persistence.model_choice_record import ModelChoiceRecord

    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        scr._cmd_model("light")
        await pilot.pause()
        (mark,) = _marks(scr)
        written = _choice_records(tmp_path)
        app.active_runner = _Runner("light")
        _write_config(tmp_path, slots={"light": "qwen-coder"})
        app.reload_config()
        await pilot.pause()
        assert _marks(scr) == [mark]
        await _assert_rule(pilot, mark, "● glm-flash")
        assert _choice_records(tmp_path) == written
        await _finish_run(pilot, app)
        last = _marks(scr)[-1]
        await _assert_rule(pilot, last, "● qwen-coder")
        assert _choice_records(tmp_path)[-1] == ModelChoiceRecord(
            "light", "qwen-coder", "glm-flash"
        )


@pytest.mark.asyncio
async def test_choice_dropped_at_run_end_is_followed(tmp_path):
    """Перечитанный во время хода конфиг убрал выбранную модель: выбор
    снимается в конце хода, черта следует за моделью следующего хода."""
    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        scr._cmd_model("qwen-coder")
        await pilot.pause()
        app.active_runner = _Runner("qwen-coder")
        _write_config(
            tmp_path,
            models={"glm-flash": {}, "glm-4.6": {}},
            slots={"light": "glm-flash", "default": "glm-flash"},
        )
        app.reload_config()
        await pilot.pause()
        await _finish_run(pilot, app)
        assert "model" not in app._session_overrides
        last = _marks(scr)[-1]
        # glm-flash теперь на двух уровнях — две точки.
        await _assert_rule(pilot, last, "●● glm-flash")


async def _run_with_slot_change(pilot, app: TuiApp, tmp_path: Path) -> _Runner:
    """Ход на `default` (glm-4.6 по конфигу старта хода); во время хода
    перечитанный конфиг делает default моделью qwen-coder."""
    runner = _Runner("default")
    app.active_runner = runner
    await pilot.pause()
    _write_config(tmp_path, slots={"default": "qwen-coder"})
    app.reload_config()
    await pilot.pause()
    return runner


@pytest.mark.asyncio
async def test_empty_trace_baseline_is_model_the_run_ran_on(tmp_path):
    """Опора пустой ленты после хода — модель, на которой ход шёл (его
    Router), а не то, во что выбор резолвит уже сменённый конфиг."""
    from gc_core.persistence.model_choice_record import ModelChoiceRecord

    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        await _run_with_slot_change(pilot, app, tmp_path)
        assert _marks(scr) == []
        await _finish_run(pilot, app)
        (mark,) = _marks(scr)
        await _assert_rule(pilot, mark, "● qwen-coder")
        assert _choice_records(tmp_path) == [
            ModelChoiceRecord("default", "qwen-coder", "glm-4.6")
        ]


@pytest.mark.asyncio
async def test_follow_waits_for_stop_thread_to_exit(tmp_path):
    """Стоп: поток хода ещё дорабатывает — сверки нет; вышел — сверка на
    тике, черта модели следующего хода."""
    app = _config_app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _attached(pilot, app)
        runner = await _run_with_slot_change(pilot, app, tmp_path)
        await _ticks(pilot, 1)
        app.last_stop_requested = True
        scr._stopping_runner = runner
        app.active_runner = None
        await _ticks(pilot)
        assert _marks(scr) == []
        runner.running = False
        await _ticks(pilot)
        (mark,) = _marks(scr)
        await _assert_rule(pilot, mark, "● qwen-coder")
