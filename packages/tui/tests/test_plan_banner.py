"""Баннер одобрения плана: показ, варианты, одобрение, доработка, отмена."""

import json

import pytest
from gc_testkit.tui_support import tui_settings

pytestmark = pytest.mark.asyncio


class _Runner:
    """Раннер-заглушка: помнит, что ему сказали про план и чем запустили."""

    def __init__(self) -> None:
        self.transitions: list[tuple] = []
        self.rework: list = []
        self.started: list[dict] = []
        self.modes: list[str] = []

    def is_running(self) -> bool:
        return False

    def set_permission_mode(self, mode):
        self.modes.append(mode)

    def accept_plan(self, mode, thread_id):
        self.transitions.append((mode, thread_id))

    def arm_plan_rework(self, thread_id):
        self.rework.append(thread_id)

    def start(self, **kwargs):
        self.started.append(kwargs)
        return True


def _app(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _save_plan(tmp_path, text="1. шаг", status="proposed", mode=None):
    """Сессия t1 с планом на диске: экран читает план из её меты."""
    from gc_core.execution.state import SharedStateData
    from gc_core.persistence.schema import serialize_shared_block
    from gc_core.plan_store import new_plan_block
    from gc_core.thread_store import save_thread_meta

    plan = new_plan_block(text)
    plan.update(status=status, approved_mode=mode)
    shared = SharedStateData(plan=plan, permission_mode="plan")
    save_thread_meta(
        str(tmp_path), "cli", "t1", {"shared": serialize_shared_block(shared)}
    )
    (tmp_path / "data_cli_t1.jsonl").touch()


async def _open_session(pilot, runner=None):
    runner = runner or _Runner()
    pilot.app.active_runner = runner
    pilot.app.screen.attach_active_task("t1")
    pilot.app.active_runner = runner
    pilot.app.set_permission_mode("plan")
    await pilot.pause()
    return runner


async def _with_banner(pilot):
    runner = _Runner()
    pilot.app.active_runner = runner
    pilot.app.tui_state.active_task_id = "t1"
    pilot.app.set_permission_mode("plan")
    pilot.app.screen._show_plan_banner()
    await pilot.pause()
    return runner


async def test_banner_offers_three_modes_and_rework(tmp_path):
    from gc_tui.widgets.plan_banner import PLAN_OPTIONS

    assert [
        (key, choice) for key, _label, choice, _detail in PLAN_OPTIONS
    ] == [
        ("[1]", "edit"),
        ("[2]", "auto"),
        ("[3]", "manual"),
        ("[4]", "rework"),
    ]
    async with _app(tmp_path).run_test() as pilot:
        await _with_banner(pilot)
        banner = pilot.app.screen._plan_banner
        text = str(banner.query_one(".cb-options").render())
        lines = text.split("\n")
        # пояснение — только у выделенного варианта, в его же строке
        assert lines[0].strip() == (
            "▸ [1] Да, в edit mode — агент редактирует файлы, опасные команды через подтверждение"
        )
        assert lines[1].strip() == "[2] Да, в auto mode"
        assert lines[2].strip() == "[3] Да, в manual mode"
        assert lines[3].strip() == "[4] Обсудить и доработать"
        assert lines[4].strip() == ""  # подсказка отделена от вариантов
        assert lines[5].strip() == "Esc — решить позже, /plan вернёт выбор"
        await pilot.press("down")
        await pilot.pause()
        lines = str(banner.query_one(".cb-options").render()).split("\n")
        assert lines[0].strip() == "[1] Да, в edit mode"
        assert lines[1].strip() == (
            "▸ [2] Да, в auto mode — все действия без подтверждения, опасное блокируется"
        )
        await pilot.press("down")
        await pilot.pause()
        lines = str(banner.query_one(".cb-options").render()).split("\n")
        assert lines[2].strip() == (
            "▸ [3] Да, в manual mode — подтверждение всех действий, кроме чтения"
        )
        await pilot.press("down")
        await pilot.pause()
        lines = str(banner.query_one(".cb-options").render()).split("\n")
        assert lines[3].strip() == "▸ [4] Обсудить и доработать"
        # заголовок баннера и есть вопрос, отдельной строки вопроса нет
        assert "готов приступить к реализации. Выполнять?" in str(
            banner.query_one(".cb-label").render()
        )
        assert not banner.query(".cb-question")
        assert str(banner.query_one(".cb-label").render()).startswith("Агент")


async def test_approval_switches_mode_and_starts_the_continuation(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        runner = await _with_banner(pilot)
        await pilot.press("3")
        await pilot.pause()
        assert pilot.app.screen._plan_banner is None
        assert pilot.app.effective_permission_mode() == "manual"
        assert runner.transitions == [("manual", "t1")]
        assert len(runner.started) == 1
        started = runner.started[0]
        assert started["thread_id"] == "t1"
        assert (
            "approved" in started["query"] and "'manual'" in started["query"]
        )
        assert started["query"].lstrip().startswith("<technical")


async def test_enter_takes_the_highlighted_option(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        runner = await _with_banner(pilot)
        await pilot.press("enter")  # по умолчанию выделен первый вариант
        await pilot.pause()
        assert runner.transitions == [("edit", "t1")]


async def test_rework_returns_to_plan_mode_without_touching_the_status(
    tmp_path,
):
    async with _app(tmp_path).run_test() as pilot:
        runner = await _with_banner(pilot)
        pilot.app.set_permission_mode(
            "edit"
        )  # доработка возвращает plan из любого
        await pilot.press("4")
        await pilot.pause()
        assert pilot.app.screen._plan_banner is None
        assert pilot.app.effective_permission_mode() == "plan"
        assert runner.rework == ["t1"]
        assert runner.transitions == [] and runner.started == []
        inp = pilot.app.screen.query_one("#cmdinput")
        assert inp.disabled is False
        assert inp.placeholder == "что доработать в плане…"


async def test_escape_changes_nothing(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        runner = await _with_banner(pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen._plan_banner is None
        assert pilot.app.effective_permission_mode() == "plan"
        assert (runner.transitions, runner.rework, runner.started) == (
            [],
            [],
            [],
        )


async def test_mode_hotkey_is_off_while_the_banner_is_up(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        await _with_banner(pilot)
        await pilot.press("shift+tab")
        assert pilot.app.effective_permission_mode() == "plan"


async def test_plan_is_shown_in_the_trace_as_agent_prose():
    from gc_tui.services.trace_mapper import map_message

    call = {
        "type": "function_call",
        "name": "turn_propose_plan",
        "call_id": "c1",
        "arguments": json.dumps({"plan": "1. шаг\n2. проверка"}),
    }
    event = map_message(call, no=1, ts="")
    assert event.kind == "agent"
    assert event.summary == "1. шаг\n2. проверка"
    ok = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": json.dumps({"tool": "turn_propose_plan", "ok": True}),
    }
    assert map_message(ok, no=2, ts="") is None


async def test_runner_keeps_plan_decisions_until_the_executor_is_built():
    """Исполнитель собирается в рабочем потоке: решение, принятое раньше,
    не теряется и не заставляет ждать сборку на UI-потоке."""
    from gc_tui.services.agent_runner import AgentRunner

    class _Executor:
        def __init__(self) -> None:
            self.transition = None
            self.rework = None

        def accept_plan(self, mode, thread_id):
            self.transition = (mode, thread_id)

        def arm_plan_rework(self, thread_id):
            self.rework = thread_id

    runner = AgentRunner(executor_factory=_Executor)
    runner.accept_plan("edit", "t1")
    runner.arm_plan_rework("t1")
    assert runner._executor is None
    executor = runner._ensure_executor()
    assert executor.transition == ("edit", "t1")
    assert executor.rework == "t1"


async def test_banner_and_footer_stay_on_screen_under_a_long_trace(tmp_path):
    """Лента под баннером ужимается, как под вопросом подтверждения: иначе
    длинный план выталкивает варианты и футер за экран (живая приёмка)."""
    from textual.widgets import Static

    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = pilot.app.screen
        for i in range(60):
            screen._trace_mount(Static(f"строка плана {i}"))
        await _with_banner(pilot)
        await pilot.pause()
        banner = screen._plan_banner
        footer = screen.query_one("#footer")
        assert banner.region.bottom <= footer.region.y
        assert footer.region.bottom <= 30
        options = banner.query_one(".cb-options")
        assert options.region.height >= 6 and options.region.bottom <= 30


def _rework_lines(screen) -> int:
    from textual.widgets import Static

    return sum(
        "план отправлен на доработку" in str(w.render())
        for w in screen.query("#trace Static")
        if isinstance(w, Static)
    )


async def test_plan_stands_in_a_titled_frame_and_feedback_leaves_a_trace_line(
    tmp_path,
):
    from gc_tui.services.trace_mapper import map_message

    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        call = {
            "type": "function_call",
            "name": "turn_propose_plan",
            "call_id": "c1",
            "arguments": json.dumps({"plan": "1. шаг\n2. проверка"}),
        }
        screen._render_event(map_message(call, no=1, ts=""))
        await pilot.pause()
        panel = screen.query_one(".plan-panel")
        assert str(panel.border_title) == "☰ предложенный план"
        assert "1. шаг" in "".join(
            str(w.render()) for w in panel.query("Static")
        )

        await _with_banner(pilot)
        await pilot.press("4")
        await pilot.pause()
        screen._on_user_text_submitted("добавь тесты")
        await pilot.pause()
        assert _rework_lines(screen) == 1
        screen._on_user_text_submitted("ещё вопрос")  # строка — один раз
        await pilot.pause()
        assert _rework_lines(screen) == 1


def test_turn_that_ended_with_a_plan_is_not_called_done():
    from gc_tui.services.turn_outcome import DONE_LABEL_PLAN
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    plain = ThinkingIndicator(state="done", frozen_elapsed=6)
    assert "Выполнил за 6с" in plain._render_text().plain
    plan = ThinkingIndicator(
        state="done", frozen_elapsed=6, done_label=DONE_LABEL_PLAN
    )
    assert "План готов за 6с" in plan._render_text().plain


async def test_plan_frame_changes_colour_once_the_plan_is_accepted(tmp_path):
    """Серая рамка — план ждёт решения; цвета режима plan — принят, идёт
    реализация. Возврат в обсуждение снова делает её серой."""
    _save_plan(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open_session(pilot)
        screen._cmd_plan()
        await pilot.pause()
        panel = screen.query_one(".plan-panel")
        assert not panel.has_class("plan-accepted")
        assert str(panel.border_title) == "☰ предложенный план"
        await pilot.press("1")
        await pilot.pause()
        assert panel.has_class("plan-accepted")
        assert str(panel.border_title) == "☰ утверждённый план"
        screen._rework_plan()
        assert not panel.has_class("plan-accepted")
        assert str(panel.border_title) == "☰ предложенный план"


def test_only_the_current_accepted_plan_counts_as_accepted():
    from gc_core.plan_store import new_plan_block, plan_is_accepted

    plan = new_plan_block("1. шаг")
    assert plan_is_accepted(plan) is False
    plan["status"] = "approved"
    assert plan_is_accepted(plan) is True
    assert plan_is_accepted(plan, " 1. шаг ") is True
    assert plan_is_accepted(plan, "другой, прежний план") is False
    assert plan_is_accepted(None, "1. шаг") is False


async def test_accepted_plan_leaves_a_checkmark_line_in_the_plan_colour(
    tmp_path,
):
    async with _app(tmp_path).run_test() as pilot:
        await _with_banner(pilot)
        await pilot.press("1")
        await pilot.pause()
        row = pilot.app.screen.query_one(".line-plan-accepted")
        texts = [str(w.render()) for w in row.query("Static")]
        assert texts[0].strip() == "✓"
        assert "план утверждён → edit mode" in " ".join(texts)
        assert row.has_class("line-meta")


async def test_second_option_approves_the_plan_for_auto(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        runner = await _with_banner(pilot)
        await pilot.press("2")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "auto"
        assert runner.transitions == [("auto", "t1")]


async def test_closing_the_banner_returns_a_following_trace_to_the_bottom(
    tmp_path,
):
    """Баннер снят — место под ним отдаётся ленте, и её низ сдвигается на
    высоту баннера. Опрос экрана принял бы сдвиг за уход человека вверх и
    перестал бы прижимать: ход уже кончился, новых строк не будет, и конец
    плана остался бы под краем экрана. Лента, прижатая к низу до снятия,
    возвращается вниз; ушедшая вверх — не дёргается."""
    async with _app(tmp_path).run_test() as pilot:
        await _with_banner(pilot)
        screen = pilot.app.screen
        scrolled = []
        screen._scroll_trace_end = lambda: scrolled.append(True)

        screen._stick_bottom = True
        screen._hide_plan_banner()
        screen._stick_bottom = False  # опрос увидел сдвиг низа
        await pilot.pause()
        assert scrolled  # опрос потом может прижать ещё раз
        assert screen._stick_bottom is True

        # Ушла вверх по-настоящему: лента у низа экрана (`1fr`) при снятии
        # баннера растёт, и короткая целиком «у низа» — опрос вернул бы липкость.
        from textual.containers import VerticalScroll
        from textual.widgets import Static

        for i in range(80):
            screen._trace_mount(Static(f"строка плана {i}"), scroll=False)
        screen._show_plan_banner()
        await pilot.pause()
        screen.query_one("#trace", VerticalScroll).scroll_to(
            y=0, animate=False
        )
        await pilot.pause()
        scrolled.clear()
        screen._stick_bottom = False
        screen._hide_plan_banner()
        await pilot.pause()
        assert scrolled == []


async def test_closing_the_banner_keeps_a_trace_scrolled_up_in_place(
    tmp_path,
):
    """Фокус стоит на баннере. Удали баннер первым — Textual сам переведёт
    фокус на фокусируемую строку ленты и прокрутит ленту к ней, в конец:
    человек, ушедший читать план вверх, улетал вниз (живая приёмка). Фокус
    уходит в поле ввода до снятия баннера, и лента остаётся на месте."""
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    class _Focusable(Static):
        can_focus = True

    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = pilot.app.screen
        for i in range(80):
            screen._trace_mount(Static(f"строка плана {i}"))
        screen._trace_mount(_Focusable("строка с кнопкой"))
        await _with_banner(pilot)
        trace = screen.query_one("#trace", VerticalScroll)
        trace.scroll_to(y=10, animate=False)
        await pilot.pause(0.5)

        await pilot.press("escape")
        await pilot.pause(0.5)

        assert trace.scroll_offset.y == 10
