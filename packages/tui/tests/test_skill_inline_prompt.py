"""inline /skill <id> <prompt>: префикс «Use skill …» в самой реплике.

Та же форма, что у pending-пути (_consume_pending_prefix) — единственная,
доказанно работающая на слабых моделях. Technical-обёртка НЕ используется:
системный промпт трактует technical-блоки как «context, not commands»,
и модель вправе игнорировать такую инструкцию (реальный кейс 2026-06-10).
"""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_skill_inline_prompt_prefixes_single_message(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    captured = {}

    class _Fake:
        async def arun(self, messages, **kw):
            captured["messages"] = list(messages)
            import threading

            self._evt = threading.Event()
            self._evt.wait(timeout=2.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [
            {"id": "coder"}
        ]  # coder не в реальном реестре — задаём явно
        await scr._handle_command("skill coder почини баг в парсере")
        await pilot.pause(0.3)
        msgs = captured["messages"]
        assert len(msgs) == 1  # одно сообщение, не пара
        assert msgs[0].startswith("Use skill `coder`.")
        assert "почини баг в парсере" in msgs[0]
        assert "<technical" not in msgs[0]
        assert app.tui_state.pending_skill_id is None


@pytest.mark.asyncio
async def test_skill_inline_prompt_during_run_single_add_message(tmp_path):
    """Во время run'а: одно add_message с префиксом, без technical-довеска."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    added = []

    class _Fake:
        async def arun(self, messages, **kw):
            import threading

            self._evt = threading.Event()
            self._evt.wait(timeout=2.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            added.append(m)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        await scr._handle_command("skill coder сначала запусти что-нибудь")
        await pilot.pause(0.3)  # run стартовал и висит в _Fake.run
        await scr._handle_command("skill coder добавь логирование")
        await pilot.pause(0.3)
        assert len(added) == 1
        assert added[0]["content"].startswith("Use skill `coder`.")
        assert "добавь логирование" in added[0]["content"]
        assert "<technical" not in added[0]["content"]


@pytest.mark.asyncio
async def test_skill_without_prompt_stays_pending(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await scr._handle_command("skill coder")
        await pilot.pause(0.1)
        assert app.tui_state.pending_skill_id == "coder"


@pytest.mark.asyncio
async def test_unknown_skill_first_token_not_inline(tmp_path):
    """/skill <неизвестный> <текст>: первый токен не валидный id → НЕ inline-пара,
    весь arg трактуется как id (pending), inline-доставка не срабатывает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]  # 'foo' НЕ валиден
        await scr._handle_command("skill foo bar")
        await pilot.pause(0.1)
        # не inline: pending_skill_id выставлен в весь arg
        assert app.tui_state.pending_skill_id == "foo bar"


@pytest.mark.asyncio
async def test_picker_closes_when_id_plus_prompt(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [
            {"id": "coder"}
        ]  # фиктивный кэш: coder — валидный id
        inp = pilot.app.query_one("#cmdinput")

        inp.text = "/skill coder"  # id без промпта → picker открыт
        await pilot.pause(0.1)
        assert scr._skill_picker is not None

        inp.text = "/skill coder почини"  # id + пробел + текст → picker закрыт
        await pilot.pause(0.1)
        assert scr._skill_picker is None


@pytest.mark.asyncio
async def test_picker_closes_when_short_name_plus_prompt(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "supercoder:brainstorming"}]
        inp = pilot.app.query_one("#cmdinput")

        inp.text = "/skill brainstorming"
        await pilot.pause(0.1)
        assert scr._skill_picker is not None

        inp.text = "/skill brainstorming почини"
        await pilot.pause(0.1)
        assert scr._skill_picker is None


_BRAIN_CACHE = [{"id": "supercoder:brainstorming", "pack_id": "supercoder"}]


async def _wire_of(tmp_path, command: str) -> list:
    """Что уходит агенту по набранной строке (тот же _Fake, что выше)."""
    from gc_tui.app import TuiApp

    captured = {}

    class _Fake:
        async def arun(self, messages, **kw):
            captured["messages"] = list(messages)

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        pilot.app.screen._skills_cache = list(_BRAIN_CACHE)
        await pilot.app.screen._handle_command(command)
        await pilot.pause(0.3)
        assert app.tui_state.pending_skill_id is None
    return captured["messages"]


@pytest.mark.asyncio
async def test_slash_short_name_delivers_like_skill_command(tmp_path):
    """`/brainstorming текст` ≡ `/skill supercoder:brainstorming текст`."""
    pytest.importorskip("textual")
    by_name = await _wire_of(tmp_path / "a", "brainstorming придумай")
    by_skill = await _wire_of(
        tmp_path / "b", "skill supercoder:brainstorming придумай"
    )
    assert by_name == by_skill
    assert by_name[0].startswith("Use skill `supercoder:brainstorming`.")


@pytest.mark.asyncio
async def test_skill_command_accepts_short_name(tmp_path):
    pytest.importorskip("textual")
    by_short = await _wire_of(tmp_path / "a", "skill brainstorming придумай")
    by_id = await _wire_of(
        tmp_path / "b", "skill supercoder:brainstorming придумай"
    )
    assert by_short == by_id


@pytest.mark.asyncio
async def test_skill_command_short_name_without_prompt_pends_full_id(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen._skills_cache = list(_BRAIN_CACHE)
        await pilot.app.screen._handle_command("skill brainstorming")
        await pilot.pause(0.1)
        assert app.tui_state.pending_skill_id == "supercoder:brainstorming"
