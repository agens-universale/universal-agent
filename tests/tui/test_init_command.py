"""/init: видимый prompt-driven прогон — роутинг + busy-guard."""
import pytest


class _RecRunner:
    """Стаб AgentRunner: пишет аргументы start; is_running настраивается."""
    def __init__(self, running: bool = False):
        self._running = running
        self.started: list[dict] = []
    def is_running(self) -> bool:
        return self._running
    def start(self, *, query, thread_id, system=None, message_id=None, **kw):
        self.started.append({"query": query, "thread_id": thread_id})
        return True


@pytest.mark.asyncio
async def test_cmd_init_busy_guard(tmp_path):
    """runner.is_running() → сообщение, start НЕ зовётся."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    rec = _RecRunner(running=True)
    app.active_runner = rec
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._cmd_init()
        await pilot.pause(0.1)
        assert rec.started == []
        text = "\n".join(str(s.render()) for s in scr.query("Static"))
        assert "дождитесь завершения" in text


@pytest.mark.asyncio
async def test_cmd_init_starts_run_with_init_prompt(tmp_path):
    """Не running → runner.start с init-промптом как query (wire), маркер /init в трейсе."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    rec = _RecRunner(running=False)
    # get_runner() должен вернуть наш стаб
    app.active_runner = rec
    app.get_runner = lambda: rec
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._cmd_init()
        await pilot.pause(0.1)
        assert len(rec.started) == 1
        assert "write_to_file" in rec.started[0]["query"]
        assert "AGENTS.md" in rec.started[0]["query"]
        text = "\n".join(str(s.render()) for s in scr.query("Static"))
        assert "/init" in text
