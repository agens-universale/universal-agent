"""Команда /plan: вернуть план и выбор, показать план при занятом агенте, убрать план."""

import pytest
from gc_testkit.tui_support import tui_settings

pytestmark = pytest.mark.asyncio

_READY = "опишите задачу или /команда"
_WAITS = "опишите задачу или /plan — вернуться к плану"


class _Runner:
    """Двойник раннера: старт ставит прогон бегущим, решение по плану
    ложится в мету треда — так, как его застаёт конец хода у настоящего
    раннера (переход применяет продолжение, мета сливается на батче)."""

    def __init__(self, running=False, threads_dir=None) -> None:
        self.running = running
        self.threads_dir = threads_dir
        self.accepted, self.rework, self.cleared, self.started = [], [], [], []

    def is_running(self) -> bool:
        return self.running

    def set_permission_mode(self, mode):
        pass

    def accept_plan(self, mode, thread_id):
        self.accepted.append((mode, thread_id))
        _approve(self.threads_dir, thread_id, mode)

    def arm_plan_rework(self, thread_id):
        self.rework.append(thread_id)

    def clear_plan(self, thread_id):
        self.cleared.append(thread_id)

    def start(self, **kwargs):
        self.started.append(kwargs)
        self.running = True
        return True


def _approve(threads_dir, thread_id, mode):
    """Статус «утверждён» в плане сессии, остальная мета — как была."""
    from gc_core.plan_store import PLAN_STATUS_APPROVED
    from gc_core.thread_store import load_thread_meta, save_thread_meta

    meta = load_thread_meta(threads_dir, "cli", thread_id)
    meta["shared"]["plan"].update(
        status=PLAN_STATUS_APPROVED, approved_mode=mode
    )
    save_thread_meta(threads_dir, "cli", thread_id, meta)


def _app(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _save(tmp_path, *, plan="1. шаг", status="proposed", mode="plan"):
    from gc_core.execution.state import SharedStateData
    from gc_core.persistence.schema import serialize_shared_block
    from gc_core.plan_store import new_plan_block
    from gc_core.thread_store import save_thread_meta

    block = None
    if plan:
        block = new_plan_block(plan)
        block.update(
            status=status,
            approved_mode="edit" if status == "approved" else None,
        )
    shared = SharedStateData(plan=block, permission_mode=mode)
    save_thread_meta(
        str(tmp_path), "cli", "t1", {"shared": serialize_shared_block(shared)}
    )
    (tmp_path / "data_cli_t1.jsonl").touch()


async def _open(pilot, *, running=False):
    runner = _Runner(running, pilot.app._threads_dir)
    pilot.app.active_runner = runner
    pilot.app.screen.attach_active_task("t1")
    pilot.app.active_runner = runner
    await pilot.pause()
    return runner


def _panels(screen):
    return list(screen.query(".plan-panel"))


def _trace_text(screen) -> str:
    from textual.widgets import Static

    return "\n".join(
        str(w.render())
        for w in screen.query("#trace Static")
        if isinstance(w, Static)
    )


def _placeholder(screen) -> str:
    return screen.query_one("#cmdinput").placeholder


async def test_plan_returns_the_frame_and_the_choice_without_touching_the_mode(
    tmp_path,
):
    _save(tmp_path, mode="edit")
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        assert len(_panels(screen)) == 1
        assert (
            screen._plan_banner is not None
            and screen._plan_banner.resumed is False
        )
        assert pilot.app.effective_permission_mode() == "edit"
        assert "/plan clear — убрать план из сессии" in _trace_text(screen)


async def test_repeated_command_does_not_duplicate_anything(tmp_path):
    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        screen._cmd_plan()  # баннер уже поднят — команда ничего не делает
        await pilot.pause()
        assert len(_panels(screen)) == 1


async def test_frame_already_last_is_not_copied_and_a_buried_one_is(tmp_path):
    from textual.widgets import Static

    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        plan = screen._session_plan()
        screen._ensure_plan_panel_last(plan)
        await pilot.pause()
        screen._ensure_plan_panel_last(plan)  # рамка последняя — копии нет
        await pilot.pause()
        assert len(_panels(screen)) == 1
        screen._trace_mount(Static("реплика после плана"))
        await pilot.pause()
        screen._ensure_plan_panel_last(plan)  # после плана что-то было
        await pilot.pause()
        assert len(_panels(screen)) == 2


async def test_all_frames_of_the_plan_change_state_together(tmp_path):
    from textual.widgets import Static

    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        runner = await _open(pilot)
        plan = screen._session_plan()
        screen._ensure_plan_panel_last(plan)
        screen._trace_mount(Static("реплика"))
        await pilot.pause()
        screen._cmd_plan()
        await pilot.pause()
        assert len(_panels(screen)) == 2
        await pilot.press("1")
        await pilot.pause()
        assert all(p.has_class("plan-accepted") for p in _panels(screen))
        assert runner.accepted == [("edit", "t1")]


async def test_busy_agent_gets_the_frame_but_no_choice(tmp_path):
    _save(tmp_path, mode="edit")
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot, running=True)
        screen._cmd_plan()
        await pilot.pause()
        assert len(_panels(screen)) == 1
        assert screen._plan_banner is None
        assert pilot.app.effective_permission_mode() == "edit"


async def test_no_plan_enters_plan_mode_or_just_answers(tmp_path):
    _save(tmp_path, plan=None, mode="edit")
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "plan"
        assert "plan mode включён" in _trace_text(screen)
        screen._cmd_plan()
        await pilot.pause()
        assert "плана пока нет — опишите задачу" in _trace_text(screen)
        assert screen._plan_banner is None and _panels(screen) == []


async def test_no_plan_enters_plan_mode_while_the_agent_works(tmp_path):
    """Плана нет, агент занят (работает или ждёт ответа на вопрос): `/plan`
    включает plan, как Shift+Tab, — ответом служит подсказка смены режима
    в футере, строки в ленте нет. Уже в plan — уведомление «плана пока нет»."""
    _save(tmp_path, plan=None, mode="edit")
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot, running=True)
        screen._cmd_plan()
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "plan"
        assert "plan mode включён" not in _trace_text(screen)
        assert screen._plan_banner is None and _panels(screen) == []


async def test_no_plan_in_plan_mode_while_busy_only_says_so(tmp_path):
    _save(tmp_path, plan=None, mode="plan")
    notices = []
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.notify = lambda message, **_: notices.append(message)
        screen = pilot.app.screen
        await _open(pilot, running=True)
        screen._cmd_plan()
        await pilot.pause()
        assert notices == ["плана пока нет"]
        assert pilot.app.effective_permission_mode() == "plan"
        assert "плана пока нет" not in _trace_text(screen)


async def test_approved_plan_is_resumed_with_its_own_question_and_message(
    tmp_path,
):
    _save(tmp_path, status="approved", mode="edit")
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        runner = await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        banner = screen._plan_banner
        assert banner.resumed is True
        assert "План уже утверждён. Продолжить выполнение?" in str(
            banner.query_one(".cb-label").render()
        )
        assert _panels(screen)[0].has_class("plan-accepted")
        await pilot.press("3")
        await pilot.pause()
        query = runner.started[0]["query"]
        assert "may be partially done" in query and "'manual'" in query
        text = _trace_text(screen)
        assert "выполнение плана продолжено → manual mode" in text
        assert "план утверждён →" not in text


async def test_clear_removes_the_plan_and_disarms_everything(tmp_path):
    from gc_core.thread_store import load_thread_meta

    _save(tmp_path, status="approved")
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        runner = await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        await pilot.press("4")  # обсудить
        await pilot.pause()
        assert screen._plan_rework_pending is True
        screen._cmd_plan("clear")
        await pilot.pause()
        shared = load_thread_meta(str(tmp_path), "cli", "t1")["shared"]
        assert "plan" not in shared and shared["plan_removed_notice"] is True
        assert runner.cleared == ["t1"]
        assert screen._plan_rework_pending is False
        assert "план убран из сессии" in _trace_text(screen)
        assert not _panels(screen)[0].has_class("plan-accepted")
        # следующее сообщение — обычное: ложной строки о доработке нет
        screen._on_user_text_submitted("привет")
        await pilot.pause()
        assert "план отправлен на доработку" not in _trace_text(screen)
        screen._cmd_plan("clear")
        await pilot.pause()
        assert "плана и так нет" in _trace_text(screen)


async def test_clear_is_refused_while_the_agent_works(tmp_path):
    from gc_core.thread_store import load_thread_meta

    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot, running=True)
        screen._cmd_plan("clear")
        await pilot.pause()
        assert "plan" in load_thread_meta(str(tmp_path), "cli", "t1")["shared"]


async def test_discuss_then_accept_leaves_no_false_rework_line(tmp_path):
    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        screen._cmd_plan()
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        screen._on_user_text_submitted("спасибо")
        await pilot.pause()
        assert "план отправлен на доработку" not in _trace_text(screen)


async def test_rework_line_needs_plan_mode_at_the_moment_of_sending(tmp_path):
    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        pilot.app.set_permission_mode("edit")  # вышел из plan до отправки
        screen._on_user_text_submitted("сделай иначе")
        await pilot.pause()
        assert "план отправлен на доработку" not in _trace_text(screen)


async def test_input_hint_follows_one_rule(tmp_path):
    """Вопрос агента > замечания к плану > план ждёт решения > обычная."""
    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        assert _placeholder(screen) == _WAITS  # план ждёт решения в plan
        screen._cmd_plan()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert _placeholder(screen) == _WAITS  # Esc не «забывает» план
        screen._cmd_plan()
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        assert _placeholder(screen) == "что доработать в плане…"
        screen._on_user_text_submitted("добавь тесты")
        await pilot.pause()
        assert (
            _placeholder(screen) == _WAITS
        )  # подсказка доработки не залипает
        pilot.app.set_permission_mode("edit")
        screen._sync_input_placeholder()
        assert _placeholder(screen) == _READY  # вне plan не напоминаем


async def test_file_path_is_shown_only_when_the_file_exists(tmp_path):
    from gc_core.thread_store import plan_file_path
    from gc_core.thread_store_file import FileThreadStore

    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        screen._cmd_plan()
        await pilot.pause()
        assert "план сохранён:" not in _trace_text(screen)
        await pilot.press("escape")
        FileThreadStore(str(tmp_path)).save_plan_file(
            "cli", "t1", screen._session_plan()
        )
        screen._trace_mount(
            __import__("textual.widgets", fromlist=["Static"]).Static("…")
        )
        screen._cmd_plan()
        await pilot.pause()
        assert "план сохранён:" in _trace_text(screen)
        assert "plan_cli_t1.md" in _trace_text(screen)
        assert plan_file_path(str(tmp_path), "cli", "t1")


async def test_turn_that_ended_with_a_plan_does_not_double_the_frame(tmp_path):
    """Ход закончился планом: рамка из события уже в ленте — выбор поднимается
    без второй копии. Если агент предъявил сохранённый план заново (событие
    рамки не даёт) — рамка ставится программой."""
    import json

    from gc_tui.services.trace_mapper import map_message

    _save(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        call = {
            "type": "function_call",
            "name": "turn_propose_plan",
            "call_id": "c1",
            "arguments": json.dumps({"plan": "1. шаг"}),
        }
        screen._render_event(map_message(call, no=1, ts=""))
        await pilot.pause()
        screen._present_session_plan()
        await pilot.pause()
        assert len(_panels(screen)) == 1 and screen._plan_banner is not None

        await pilot.press("escape")
        shown_again = dict(
            call,
            call_id="c2",
            arguments=json.dumps({"plan": "", "show_current": True}),
        )
        assert (
            map_message(shown_again, no=2, ts="") is None
        )  # событие без рамки
        screen._trace_mount(
            __import__("textual.widgets", fromlist=["Static"]).Static(
                "реплика"
            )
        )
        screen._present_session_plan()
        await pilot.pause()
        assert len(_panels(screen)) == 2 and screen._plan_banner is not None


def test_plan_command_offers_no_argument_menu():
    """Меню аргументов выделяет первый вариант: Enter после `/plan` выполнил бы
    `clear` и стёр план, который человек хотел увидеть (живая приёмка)."""
    from gc_tui.commands import build_default_registry

    command = build_default_registry().lookup("plan")
    assert command is not None
    assert command.arg_options == ()
    assert command.arg_options_provider is None


async def test_palette_enter_on_plan_shows_the_plan_at_once(tmp_path):
    """`/plan` из палитры: Enter выполняет сразу, а не подставляет `/plan `
    в поле (§10.2); `clear` набирается руками."""
    from gc_tui.widgets.command_palette import CommandPalette

    _save(tmp_path, mode="edit")
    async with _app(tmp_path).run_test() as pilot:
        screen = pilot.app.screen
        await _open(pilot)
        screen.query_one("#cmdinput").focus()
        await pilot.press("/", "p", "l")
        await pilot.press("enter")
        await pilot.pause()
        assert not pilot.app.query(CommandPalette)
        assert screen.query_one("#cmdinput").text == ""
        assert len(_panels(screen)) == 1
        assert screen._plan_banner is not None
