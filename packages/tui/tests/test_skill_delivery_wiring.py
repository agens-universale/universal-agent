from gc_testkit.tui_support import tui_settings
import pytest


@pytest.fixture(autouse=True)
def _skills_available(monkeypatch):
    """Скиллы тестов загружаемы; иное — явным monkeypatch в тесте."""
    from gc_core.tools.skills import skill_delivery as sd

    async def _available(skill_id, reg):
        return True

    monkeypatch.setattr(sd, "skill_is_available", _available)


def _as_coro(fn):
    """Обернуть синхронный двойник в корутину."""

    async def _wrapped(*a, **kw):
        return fn(*a, **kw)

    return _wrapped


@pytest.mark.asyncio
async def test_consume_pending_prefix_injects_delivery(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_core.tools.skills import registry as sreg
    from gc_core.tools.skills import skill_delivery as sd
    from gc_tui.app import TuiApp

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(
        sd,
        "build_skill_delivery",
        # Двойник повторяет ФОРМУ: настоящая функция корутинная.
        _as_coro(lambda skill_id, reg: f"DELIVERY[{skill_id}]"),
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.app.tui_state.pending_skill_id = "qa-req-validation"
        query, patch = await scr._consume_pending_prefix("проверь требование")
        assert query == "DELIVERY[qa-req-validation]\nпроверь требование"
        assert patch == {"skills": ["qa-req-validation"]}
        assert scr.app.tui_state.pending_skill_id is None  # сброшен


@pytest.mark.asyncio
async def test_consume_pending_prefix_passthrough_without_skill(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.app.tui_state.pending_skill_id = None
        query, patch = await scr._consume_pending_prefix("просто вопрос")
        assert query == "просто вопрос"
        assert patch == {}


@pytest.mark.asyncio
async def test_cmd_use_skill_during_run_injects_delivery(
    tmp_path, monkeypatch
):
    """Во время run'а: /skill <id> шлёт add_message с инжектнутой доставкой."""
    pytest.importorskip("textual")
    from gc_core.tools.skills import registry as sreg
    from gc_core.tools.skills import skill_delivery as sd
    from gc_tui.app import TuiApp

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(
        sd,
        "build_skill_delivery",
        # Двойник повторяет ФОРМУ: настоящая функция корутинная.
        _as_coro(lambda skill_id, reg: f"DELIVERY[{skill_id}]"),
    )

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
        await scr._handle_command(
            "skill coder сначала запусти что-нибудь"
        )  # стартует run (inline)
        for _ in range(60):
            r = scr.app.active_runner
            if r is not None and r.is_running():
                break
            await pilot.pause(0.05)
        await scr._cmd_use_skill("coder")  # /skill во время run'а
        await pilot.pause(0.1)
        assert any(m.get("content") == "DELIVERY[coder]" for m in added), added


@pytest.mark.asyncio
async def test_inline_skill_display_stays_clean(tmp_path, monkeypatch):
    """inline /skill <id> <prompt>: wire содержит DELIVERY, дисплей — чистый промпт."""
    pytest.importorskip("textual")
    from gc_core.tools.skills import registry as sreg
    from gc_core.tools.skills import skill_delivery as sd
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(
        sd,
        "build_skill_delivery",
        # Двойник повторяет ФОРМУ: настоящая функция корутинная.
        _as_coro(lambda skill_id, reg: "DELIVERY"),
    )

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
        scr._skills_cache = [{"id": "coder"}]
        await scr._handle_command("skill coder чистый промпт")
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


class _ChildInputRunner:
    """Идущий прогон, ввод которого сейчас уходит субагенту."""

    def __init__(self):
        self.added: list[dict] = []

    def is_running(self) -> bool:
        return True

    def is_child_input_active(self) -> bool:
        return True

    def add_message(self, message: dict) -> None:
        self.added.append(message)


def _child_input_app(tmp_path, monkeypatch, *, available: bool):
    from gc_core.tools.skills import registry as sreg
    from gc_core.tools.skills import skill_delivery as sd
    from gc_tui.app import TuiApp

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(
        sd, "build_skill_delivery", _as_coro(lambda skill_id, reg: "DLV")
    )
    monkeypatch.setattr(
        sd, "skill_is_available", _as_coro(lambda skill_id, reg: available)
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app.active_runner = _ChildInputRunner()
    return app


@pytest.mark.asyncio
async def test_skill_prompt_to_subagent_resets_delivery_marker(
    tmp_path, monkeypatch
):
    """`/skill X текст` ушёл субагенту: реплики родителя нет, и итог
    доставки не прилипает к следующей реплике родителя."""
    pytest.importorskip("textual")
    from gc_tui.widgets.user_reply import UserReplyContainer

    app = _child_input_app(tmp_path, monkeypatch, available=True)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await scr._deliver_skill_with_prompt("coder", "сделай")
        await pilot.pause()
        assert app.active_runner.added[0]["skill_loaded"] == "coder"
        assert scr._pending_skill_marker is None
        assert list(scr.query(UserReplyContainer)) == []
        scr._mount_user_reply("потом", state="pending")
        await pilot.pause()
        (reply,) = scr.query(UserReplyContainer)
        assert reply._skill is None


@pytest.mark.asyncio
async def test_skill_only_reply_to_subagent_is_not_drawn(
    tmp_path, monkeypatch
):
    """`/skill X` без текста субагенту: загруженный — без реплики
    родителя; ненайденный — агенту не уходит, реплики нет."""
    pytest.importorskip("textual")
    from gc_tui.widgets.user_reply import UserReplyContainer

    from gc_core.tools.skills import skill_delivery as sd

    app = _child_input_app(tmp_path, monkeypatch, available=True)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await scr._cmd_use_skill("coder")
        await pilot.pause()
        assert list(scr.query(UserReplyContainer)) == []
        assert scr._pending_skill_marker is None
        monkeypatch.setattr(
            sd, "skill_is_available", _as_coro(lambda skill_id, reg: False)
        )
        await scr._cmd_use_skill("nope")
        await pilot.pause()
        # Ненайденный без текста — агенту ничего, реплики нет.
        assert list(scr.query(UserReplyContainer)) == []
        assert [m["content"] for m in app.active_runner.added] == ["DLV"]
