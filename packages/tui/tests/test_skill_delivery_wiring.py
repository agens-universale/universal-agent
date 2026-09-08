import pytest


@pytest.mark.asyncio
async def test_consume_pending_prefix_injects_delivery(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_core.tools.skills import skill_delivery as sd
    from gc_core.tools.skills import registry as sreg

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: f"DELIVERY[{skill_id}]")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.app.tui_state.pending_skill_id = "qa-req-validation"
        query, patch = scr._consume_pending_prefix("проверь требование")
        assert query == "DELIVERY[qa-req-validation]\nпроверь требование"
        assert patch == {"skills": ["qa-req-validation"]}
        assert scr.app.tui_state.pending_skill_id is None   # сброшен


@pytest.mark.asyncio
async def test_consume_pending_prefix_passthrough_without_skill(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.app.tui_state.pending_skill_id = None
        query, patch = scr._consume_pending_prefix("просто вопрос")
        assert query == "просто вопрос"
        assert patch == {}


@pytest.mark.asyncio
async def test_cmd_use_skill_during_run_injects_delivery(tmp_path, monkeypatch):
    """Во время run'а: /skill <id> шлёт add_message с инжектнутой доставкой."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_core.tools.skills import skill_delivery as sd
    from gc_core.tools.skills import registry as sreg

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: f"DELIVERY[{skill_id}]")

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

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        scr._handle_command("skill coder сначала запусти что-нибудь")   # стартует run (inline)
        for _ in range(60):
            r = scr.app.active_runner
            if r is not None and r.is_running():
                break
            await pilot.pause(0.05)
        scr._cmd_use_skill("coder")                                     # /skill во время run'а
        await pilot.pause(0.1)
        assert any(m.get("content") == "DELIVERY[coder]" for m in added), added


@pytest.mark.asyncio
async def test_inline_skill_display_stays_clean(tmp_path, monkeypatch):
    """inline /skill <id> <prompt>: wire содержит DELIVERY, дисплей — чистый промпт."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer
    from gc_core.tools.skills import skill_delivery as sd
    from gc_core.tools.skills import registry as sreg

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: "DELIVERY")

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

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        scr._handle_command("skill coder чистый промпт")
        await pilot.pause(0.3)

        # Wire: сообщение агенту содержит DELIVERY и чистый промпт
        msgs = captured.get("messages", [])
        assert len(msgs) == 1
        assert "DELIVERY" in msgs[0]
        assert "чистый промпт" in msgs[0]

        # Display: UserReplyContainer показывает чистый промпт, не DELIVERY
        replies = list(pilot.app.query(UserReplyContainer))
        assert len(replies) == 1
        assert "чистый промпт" in replies[0]._text
        assert "DELIVERY" not in replies[0]._text
