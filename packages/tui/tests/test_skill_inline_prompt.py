"""inline /skill <id> <prompt>: префикс «Use skill …» в самой реплике.

Та же форма, что у pending-пути (_consume_pending_prefix) — единственная,
доказанно работающая на слабых моделях. Technical-обёртка НЕ используется:
системный промпт трактует technical-блоки как «context, not commands»,
и модель вправе игнорировать такую инструкцию (реальный кейс 2026-06-10).
"""
import pytest


@pytest.mark.asyncio
async def test_skill_inline_prompt_prefixes_single_message(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    captured = {}

    class _Fake:
        def run(self, messages, **kw):
            captured["messages"] = list(messages)
            import threading
            self._evt = threading.Event()
            self._evt.wait(timeout=2.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]  # coder не в реальном реестре — задаём явно
        scr._handle_command("skill coder почини баг в парсере")
        await pilot.pause(0.3)
        msgs = captured["messages"]
        assert len(msgs) == 1                  # одно сообщение, не пара
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
        def run(self, messages, **kw):
            import threading
            self._evt = threading.Event()
            self._evt.wait(timeout=2.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            added.append(m)

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        scr._handle_command("skill coder сначала запусти что-нибудь")
        await pilot.pause(0.3)                  # run стартовал и висит в _Fake.run
        scr._handle_command("skill coder добавь логирование")
        await pilot.pause(0.3)
        assert len(added) == 1
        assert added[0]["content"].startswith("Use skill `coder`.")
        assert "добавь логирование" in added[0]["content"]
        assert "<technical" not in added[0]["content"]


@pytest.mark.asyncio
async def test_skill_without_prompt_stays_pending(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._handle_command("skill coder")
        await pilot.pause(0.1)
        assert app.tui_state.pending_skill_id == "coder"


@pytest.mark.asyncio
async def test_unknown_skill_first_token_not_inline(tmp_path):
    """/skill <неизвестный> <текст>: первый токен не валидный id → НЕ inline-пара,
    весь arg трактуется как id (pending), inline-доставка не срабатывает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]  # 'foo' НЕ валиден
        scr._handle_command("skill foo bar")
        await pilot.pause(0.1)
        # не inline: pending_skill_id выставлен в весь arg
        assert app.tui_state.pending_skill_id == "foo bar"


@pytest.mark.asyncio
async def test_picker_closes_when_id_plus_prompt(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]   # фиктивный кэш: coder — валидный id
        inp = pilot.app.query_one("#cmdinput")

        inp.text = "/skill coder"          # id без промпта → picker открыт
        await pilot.pause(0.1)
        assert scr._skill_picker is not None

        inp.text = "/skill coder почини"    # id + пробел + текст → picker закрыт
        await pilot.pause(0.1)
        assert scr._skill_picker is None
