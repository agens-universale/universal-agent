from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_ctrl_c_idle_requests_exit_not_stop(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = app.screen
        called = {"exit": 0, "stop": 0}
        monkeypatch.setattr(
            scr,
            "_request_exit",
            lambda: called.__setitem__("exit", called["exit"] + 1),
        )
        monkeypatch.setattr(
            scr,
            "_cmd_stop",
            lambda: called.__setitem__("stop", called["stop"] + 1),
        )
        monkeypatch.setattr(app, "_current_selection", lambda: "")
        app.action_interrupt()  # нет run, нет выделения
        assert called == {"exit": 1, "stop": 0}  # exit-модалка, НЕ стоп/спам


def _rows(app) -> list[str]:
    return [
        "".join(seg.text for seg in strip).rstrip()
        for strip in app.screen._compositor.render_strips()
    ]


@pytest.mark.asyncio
async def test_brand_header_shows_the_version_beside_the_mark(monkeypatch):
    """Версия — справа от нижней строки знака, под «Universal Agent»: рядом
    с ней встаёт и хвост «доступна новая версия»."""
    from gc_core import app_version

    from gc_tui import branding

    monkeypatch.setattr(
        app_version,
        "install_source",
        lambda: app_version.InstallSource("u", "master", "c", False),
    )
    monkeypatch.setattr(app_version, "agent_version", lambda: "9.9.9")
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"agent.model": "M"}))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        rows = _rows(app)
        top = next(i for i, r in enumerate(rows) if branding.MARK[0] in r)
        assert rows[top].endswith(branding.MARK[0] + "   Universal Agent")
        assert rows[top + 1].endswith(branding.MARK[1] + "   v9.9.9")


@pytest.mark.asyncio
async def test_status_goes_under_the_mark_after_a_blank_line():
    """Статус всегда под знаком, через пустую строку, и начинается в той же
    колонке, что и знак (колонка `●` ленты), без рамки вокруг."""
    from gc_tui import branding
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"agent.model": "M"}))
    async with app.run_test(size=(96, 30)) as pilot:
        await pilot.pause()
        rows = _rows(app)
        mark = next(i for i, r in enumerate(rows) if branding.MARK[1] in r)
        status = rows[mark + 2]
        assert rows[mark + 1].strip() == ""
        assert status.strip() == "✓ готов к работе"
        indent = len(status) - len(status.lstrip())
        assert indent == rows[mark].index(branding.MARK[1])
        assert chr(9474) not in "".join(rows[: mark + 3])  # рамки нет


@pytest.mark.asyncio
async def test_brand_header_relayouts_when_the_window_is_resized():
    """Шапка меряет свою ширину при отрисовке: сузили окно уже после старта —
    знак сменился однострочным заголовком, вернули ширину — знак вернулся."""
    from gc_tui import branding
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"agent.model": "M"}))
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        assert any(branding.MARK[0] in r for r in _rows(app))

        await pilot.resize_terminal(40, 34)
        await pilot.pause()
        await pilot.pause()
        rows = _rows(app)
        assert not any(branding.MARK[0] in r for r in rows)
        assert any(r.strip().startswith("GigaCode  Universal") for r in rows)
        assert any(r.strip() == "✓ готов к работе" for r in rows)

        await pilot.resize_terminal(110, 34)
        await pilot.pause()
        await pilot.pause()
        assert any(branding.MARK[0] in r for r in _rows(app))
