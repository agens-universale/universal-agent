"""Task 4: write-sites выставляют display_content/skill_loaded на skill-сообщениях.

Тест проверяет контракт write-site'ов: runner.start вызывается с правильными
display_query и skill_loaded, — вместо реального прогона с бэкендом (требует
сети). Параллельно проверяется add_message-путь (inline во время run).
"""
import pytest


@pytest.mark.asyncio
async def test_skill_prompt_start_gets_display_and_skill(tmp_path, monkeypatch):
    """auto-create: runner.start получает display_query/skill_loaded."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_core.tools.skills import skill_delivery as sd
    from gc_core.tools.skills import registry as sreg

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: "WIRE")
    monkeypatch.setattr(sd, "skill_is_available", lambda skill_id, reg: True)

    captured_start: dict = {}

    class _Fake:
        async def arun(self, messages, *, display_contents=None, skills_loaded=None, **kw):
            captured_start["display_contents"] = display_contents
            captured_start["skills_loaded"] = skills_loaded
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
        scr._skills_cache = [{"id": "qa-x"}]
        scr._handle_command("skill qa-x давай обсудим")
        await pilot.pause(0.4)

    assert captured_start.get("display_contents") is not None, \
        "display_contents не передан в executor.run"
    assert captured_start["display_contents"][0] == "давай обсудим", \
        f"display_contents[0]={captured_start['display_contents'][0]!r}, ожидали 'давай обсудим'"
    assert captured_start["skills_loaded"][0] == "qa-x", \
        f"skills_loaded[0]={captured_start['skills_loaded'][0]!r}, ожидали 'qa-x'"


@pytest.mark.asyncio
async def test_skill_continuation_start_gets_display_and_skill(tmp_path, monkeypatch):
    """continuation (задача есть, run не идёт): runner.start получает display_query/skill_loaded."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_core.tools.skills import skill_delivery as sd
    from gc_core.tools.skills import registry as sreg

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: "WIRE")
    monkeypatch.setattr(sd, "skill_is_available", lambda skill_id, reg: True)

    run_calls: list = []

    class _Fake:
        async def arun(self, messages, *, display_contents=None, skills_loaded=None, **kw):
            run_calls.append({
                "display_contents": display_contents,
                "skills_loaded": skills_loaded,
            })
            import threading
            self._evt = threading.Event()
            self._evt.wait(timeout=0.1)  # быстро завершаем первый run

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "qa-x"}]
        # первый message — auto-create, run стартует и тут же завершается
        scr._handle_command("skill qa-x первый запрос")
        await pilot.pause(0.4)
        # второй message — continuation (задача есть, run уже завершён)
        scr._handle_command("skill qa-x второй запрос")
        await pilot.pause(0.4)

    assert len(run_calls) >= 2, f"ожидали 2 run_calls, получили {len(run_calls)}"
    second = run_calls[1]
    assert second["display_contents"] is not None
    assert second["display_contents"][0] == "второй запрос", \
        f"display_contents[0]={second['display_contents'][0]!r}"
    assert second["skills_loaded"][0] == "qa-x", \
        f"skills_loaded[0]={second['skills_loaded'][0]!r}"


@pytest.mark.asyncio
async def test_skill_inline_during_run_add_message_gets_fields(tmp_path, monkeypatch):
    """inline /skill во время run: add_message получает display_content и skill_loaded."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_core.tools.skills import skill_delivery as sd
    from gc_core.tools.skills import registry as sreg

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: "WIRE")
    monkeypatch.setattr(sd, "skill_is_available", lambda skill_id, reg: True)

    added: list = []

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

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "qa-x"}]
        # запустить run
        scr._handle_command("skill qa-x стартовое сообщение")
        await pilot.pause(0.3)
        # во время run — inline skill c промптом
        scr._handle_command("skill qa-x добавь тест")
        await pilot.pause(0.3)

    skill_msgs = [m for m in added if m.get("role") == "user"]
    assert skill_msgs, "add_message не получил ни одного user-сообщения"
    msg = skill_msgs[0]
    assert "display_content" in msg, \
        f"display_content отсутствует в add_message: {msg}"
    assert msg["display_content"] == "добавь тест", \
        f"display_content={msg['display_content']!r}"
    assert msg.get("skill_loaded") == "qa-x", \
        f"skill_loaded={msg.get('skill_loaded')!r}"
