"""/init-команда: роутинг, busy-guard и рендер результата."""
import threading

import pytest


def _trace_text(screen) -> str:
    """Собрать весь текст из трейс-виджетов на экране."""
    from textual.widgets import Static
    return "\n".join(str(s.render()) for s in screen.query(Static))


class _FakeRunner:
    """Минимальный заглушка AgentRunner для unit-тестов без Textual."""
    def __init__(self, running: bool = False):
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def _ensure_executor(self):
        raise RuntimeError("_ensure_executor не должен вызываться в этом тесте")


# ---------------------------------------------------------------------------
# Test 1: роутинг — _dispatch_command("init") вызывает _cmd_init
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_init_calls_cmd_init(tmp_path):
    """_dispatch_command('init') вызывает _cmd_init (не молчит)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    called = {"flag": False}

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen

        # Монкипатчим _cmd_init на экранном объекте, чтобы не запускать агента
        def _fake_cmd_init():
            called["flag"] = True

        scr._cmd_init = _fake_cmd_init

        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/init"
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert called["flag"], "_dispatch_command('init') должен был вызвать _cmd_init"


# ---------------------------------------------------------------------------
# Test 2: busy-guard — не запускает поток и показывает ошибку
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cmd_init_busy_guard(tmp_path, monkeypatch):
    """Если runner.is_running() == True, _cmd_init показывает сообщение, не стартует поток."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    ensure_called = {"flag": False}

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    # Фейковый раннер: is_running = True
    fake_runner = _FakeRunner(running=True)
    app.active_runner = fake_runner

    # ensure_wired_ctx не должен вызываться
    def _bad_ensure():
        ensure_called["flag"] = True
        raise AssertionError("ensure_wired_ctx не должен вызываться при busy")

    monkeypatch.setattr(app, "ensure_wired_ctx", _bad_ensure)

    threads_before = {t.name for t in threading.enumerate()}

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._cmd_init()
        await pilot.pause(0.1)

        # Нет нового потока gc-ua-init
        threads_after = {t.name for t in threading.enumerate()}
        new_threads = threads_after - threads_before
        assert "gc-ua-init" not in new_threads

        # Сообщение об ожидании смонтировано
        text = _trace_text(scr)
        assert "дождитесь завершения" in text

    assert not ensure_called["flag"]


# ---------------------------------------------------------------------------
# Test 3: _show_init_result — рендер всех статусов
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_show_init_result_written(tmp_path):
    """`status='written'` → строка с путём и режимом."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_init_result({"status": "written", "path": "/x/AGENTS.md", "mode": "fresh"})
        await pilot.pause(0.1)
        text = _trace_text(scr)
        assert "AGENTS.md записан" in text
        assert "/x/AGENTS.md" in text
        assert "fresh" in text


@pytest.mark.asyncio
async def test_show_init_result_empty(tmp_path):
    """`status='empty'` → строка об ошибке пустого контента."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_init_result({"status": "empty", "path": "/x/AGENTS.md", "mode": "fresh"})
        await pilot.pause(0.1)
        text = _trace_text(scr)
        assert "агент вернул пусто" in text
        assert "AGENTS.md не записан" in text


@pytest.mark.asyncio
async def test_show_init_result_cancelled(tmp_path):
    """`status='cancelled'` → строка об отмене."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_init_result({"status": "cancelled"})
        await pilot.pause(0.1)
        text = _trace_text(scr)
        assert "отменено" in text


@pytest.mark.asyncio
async def test_show_init_result_error(tmp_path):
    """`status='error'` → строка с текстом исключения."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_init_result({"status": "error", "error": "connection refused"})
        await pilot.pause(0.1)
        text = _trace_text(scr)
        assert "ошибка" in text
        assert "connection refused" in text
