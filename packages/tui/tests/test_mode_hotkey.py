"""Shift+Tab переключает режим разрешений; выбор доезжает до движка."""

import pytest
from gc_testkit.tui_support import tui_settings
from footer_text import footer_rows

pytestmark = pytest.mark.asyncio


def _app(tmp_path, **config):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M", **config}),
    )


def _footer(pilot) -> str:
    """Строка 1 футера — там живёт пилюля режима."""
    return footer_rows(pilot.app)[0]


async def test_shift_tab_cycles_modes_and_the_pill_follows(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        assert pilot.app.effective_permission_mode() == "edit"
        assert pilot.app.permission_mode_source() == "default"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "manual"
        assert pilot.app.permission_mode_source() == "session"
        assert _footer(pilot).startswith("⏸ manual mode")
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "plan"
        assert _footer(pilot).startswith("☰ plan mode")
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "auto"
        assert _footer(pilot).startswith("⚡ auto mode")
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "edit"
        assert _footer(pilot).startswith("▸▸ edit mode")


async def test_mode_hint_sits_right_after_the_pill(tmp_path):
    """Смена режима на 2,5 с дописывает пояснение одной фразой с пилюлей —
    с переносом из строки 1 в строку 2; хвост и бейдж на это время скрыты,
    потом возвращаются."""
    async with _app(tmp_path).run_test(size=(64, 24)) as pilot:
        pilot.app._update_available = True  # непустой бейдж
        scr = pilot.app.screen
        await pilot.press("shift+tab")
        await pilot.pause()
        mode, slot = footer_rows(pilot.app)
        assert mode.startswith("⏸ manual mode — ")
        assert slot, "пояснение не перенеслось во вторую строку"
        assert f"{mode} {slot}" == (
            "⏸ manual mode — подтверждение всех действий, кроме чтения"
        )
        assert scr._footer_hint_timer is not None
        scr._clear_footer_hint()  # таймер 2,5 с истёк
        await pilot.pause()
        assert footer_rows(pilot.app) == (
            "⏸ manual mode (Shift+Tab)",
            "ℹ 1 · F4/notices",
        )


async def test_hotkey_disabled_outside_main_screen(tmp_path):
    """На /settings пилюли нет — клавиша не должна молча менять режим."""
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("f6")
        await pilot.pause()
        await pilot.press("shift+tab")
        assert pilot.app.effective_permission_mode() == "edit"


async def test_hotkey_disabled_while_confirm_banner_visible(tmp_path):
    """Смысл вопроса подтверждения зависит от режима: посреди него режим не меняют."""
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.screen.show_confirm_banner("t", "q", [])
        await pilot.pause()
        await pilot.press("shift+tab")
        assert pilot.app.effective_permission_mode() == "edit"


async def test_hotkey_stays_enabled_during_ask_human(tmp_path):
    """Вопрос агента режима не касается: человек ужесточает режим ПЕРЕД ответом."""
    from gc_tui.state import AskHumanState

    async with _app(tmp_path).run_test() as pilot:
        pilot.app.tui_state.ask_human_state = AskHumanState(
            call_id="c1", question="Q?"
        )
        await pilot.pause()
        await pilot.press("shift+tab")
        assert pilot.app.effective_permission_mode() == "manual"


async def test_config_default_mode_used_when_no_session_choice(tmp_path):
    app = _app(tmp_path, **{"security.permissions.default_mode": "manual"})
    async with app.run_test() as pilot:
        assert pilot.app.effective_permission_mode() == "manual"
        assert pilot.app.permission_mode_source() == "config"
        assert _footer(pilot).startswith("⏸ manual mode")


async def test_thread_meta_mode_adopted(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.adopt_thread_permission_mode("manual")
        assert pilot.app.effective_permission_mode() == "manual"


async def test_unknown_mode_in_thread_meta_is_ignored(tmp_path):
    """Мета из другой сборки или режим, которого больше нет: движок такой
    режим отвергает исключением, и открытие сессии не должно на нём падать."""
    async with _app(tmp_path).run_test() as pilot:
        sent = []
        pilot.app.set_permission_mode = sent.append
        pilot.app.adopt_thread_permission_mode("bypass")
        assert sent == []
        pilot.app.adopt_thread_permission_mode(" Manual ")
        assert sent == ["manual"]


async def test_startup_flag_wins_over_thread_meta(tmp_path):
    app = _app(tmp_path)
    app.lock_permission_mode("manual")  # эквивалент --permission-mode
    async with app.run_test() as pilot:
        pilot.app.adopt_thread_permission_mode("edit")
        assert pilot.app.effective_permission_mode() == "manual"


class _FakeExecutor:
    """Движок: помнит режим так же, как AgentExecutor.set_permission_mode."""

    def __init__(self) -> None:
        self.mode = None

    def set_permission_mode(self, mode: str) -> None:
        self.mode = mode

    def clear_permission_mode(self) -> None:
        self.mode = None


def _engine_mode(app) -> str | None:
    runner = app.get_runner()
    runner._ensure_executor()
    return runner._executor.mode


async def test_mode_reaches_engine_of_freshly_built_runner(tmp_path):
    """Раннера в момент хоткея может не быть (после /stop, перечитки конфига):
    свежий раннер обязан получить текущий выбор при создании."""
    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    async with app.run_test() as pilot:
        await pilot.press("shift+tab")
        pilot.app.invalidate_runner()
        assert _engine_mode(pilot.app) == "manual"


async def test_mode_reaches_engine_of_live_runner(tmp_path):
    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    async with app.run_test() as pilot:
        assert (
            _engine_mode(pilot.app) is None
        )  # выбора не было — движок на конфиге
        await pilot.press("shift+tab")
        assert _engine_mode(pilot.app) == "manual"


async def test_startup_flag_and_thread_meta_reach_engine(tmp_path):
    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    app.lock_permission_mode("manual")
    async with app.run_test() as pilot:
        assert _engine_mode(pilot.app) == "manual"

    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    async with app.run_test() as pilot:
        pilot.app.adopt_thread_permission_mode("manual")
        assert _engine_mode(pilot.app) == "manual"


def _save_session(tmp_path, mode=None):
    from gc_core.execution.state import SharedStateData
    from gc_core.persistence.schema import serialize_shared_block
    from gc_testkit.develop_files import save_thread_meta

    shared = SharedStateData(permission_mode=mode)
    save_thread_meta(
        str(tmp_path), "cli", "t1", {"shared": serialize_shared_block(shared)}
    )
    (tmp_path / "data_cli_t1.jsonl").touch()


async def test_session_without_a_mode_takes_the_config_default(tmp_path):
    """Окно в auto открывает старую сессию без режима: действует default_mode
    конфига, и в движке выбора окна больше нет."""
    app = _app(tmp_path, **{"security.permissions.default_mode": "manual"})
    app._executor_factory = _FakeExecutor
    async with app.run_test() as pilot:
        pilot.app.set_permission_mode("auto")
        assert _engine_mode(pilot.app) == "auto"
        pilot.app.adopt_thread_permission_mode(None, resumed=True)
        assert pilot.app.effective_permission_mode() == "manual"
        assert pilot.app.permission_mode_source() == "config"
        assert _engine_mode(pilot.app) is None


async def test_unknown_saved_mode_is_treated_as_none(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.set_permission_mode("auto")
        pilot.app.adopt_thread_permission_mode("bypass", resumed=True)
        assert pilot.app.effective_permission_mode() == "edit"
        assert pilot.app.permission_mode_source() == "default"


async def test_startup_flag_survives_a_session_without_a_mode(tmp_path):
    app = _app(tmp_path)
    app.lock_permission_mode("auto")
    async with app.run_test() as pilot:
        pilot.app.adopt_thread_permission_mode(None, resumed=True)
        assert pilot.app.effective_permission_mode() == "auto"


async def test_picker_resume_without_a_mode_drops_the_window_mode(tmp_path):
    _save_session(tmp_path)
    app = _app(tmp_path, **{"security.permissions.default_mode": "manual"})
    async with app.run_test() as pilot:
        pilot.app.set_permission_mode("auto")
        assert pilot.app.screen.attach_active_task("t1", is_resume=True)
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "manual"


async def test_picker_resume_with_a_mode_brings_it(tmp_path):
    _save_session(tmp_path, mode="plan")
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.set_permission_mode("auto")
        assert pilot.app.screen.attach_active_task("t1", is_resume=True)
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "plan"


async def test_auto_created_thread_keeps_the_window_mode(tmp_path):
    """Первое сообщение создаёт тред окна (как /new): режим окна остаётся."""
    _save_session(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.set_permission_mode("auto")
        pilot.app.screen.attach_active_task("t1")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "auto"
