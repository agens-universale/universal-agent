import pytest


def _content(widget) -> str:
    # Способ чтения контента Static в этом проекте (как в других tui-тестах).
    return str(widget.render())


def _stub_factory(app) -> None:
    # Тест управляет app._mcp_progress сам; без стаба warm() из on_mount строит
    # реальный executor и перезаписывает sink (_build_real_executor) — гонка/флак.
    app._executor_factory = lambda: object()


@pytest.mark.asyncio
async def test_indicator_failed_settles_to_persistent_line(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.mcp_progress import McpProgress
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    _stub_factory(app)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        prog = McpProgress()
        prog.begin(["computer", "office"])
        app._mcp_progress = prog
        scr._update_mcp_indicator()
        await pilot.pause()
        assert scr._mcp_status_row is not None
        body = _content(scr._mcp_status_row)
        assert "office" in body and "⏳" in body   # многострочный блок, office грузится
        # office упал, computer ок → all_reported → сначала финальный блок со
        # статусами (лингер 1с, видно что все проверены), потом строка с /mcp.
        prog.update("computer", {"status": "connected"})
        prog.update("office", {"status": "failed", "reason": "x"})
        scr._update_mcp_indicator()
        await pilot.pause()
        assert scr._mcp_status_done is True
        assert app._mcp_progress is None                  # sink погашен (показ один раз)
        body = _content(scr._mcp_status_row)
        assert "✓" in body and "✗" in body                # блок с итоговыми глифами
        assert "/mcp" not in body                          # предупреждение ещё не показано
        # после лингера блок схлопывается в постоянный итог:
        # статус MCP + строка готовности (запись в шапке).
        await pilot.pause(1.2)
        body = _content(scr._mcp_status_row)
        assert "/mcp" in body
        assert "готов к работе" in body


@pytest.mark.asyncio
async def test_indicator_warning_survives_trace_rebuild(tmp_path):
    """Предупреждение о недоступных MCP — часть стартовой шапки (как бренд):
    переживает пересборку #trace (первое сообщение / new / resume / stream_reset)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.mcp_progress import McpProgress
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    _stub_factory(app)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        prog = McpProgress()
        prog.begin(["office"])
        app._mcp_progress = prog
        scr._update_mcp_indicator()
        prog.update("office", {"status": "failed", "reason": "x"})
        scr._update_mcp_indicator()
        await pilot.pause(1.2)  # лингер финального блока → строка-предупреждение
        assert "/mcp" in _content(scr._mcp_status_row)
        # Пересборка трейса (все точки идут через _clear_trace)
        scr._switch_to_empty_state()
        await pilot.pause()
        # MCP-статус теперь живёт внутри #brand-block как #brand-mcp (A2).
        rows = pilot.app.query("#brand-mcp")
        assert len(rows) == 1
        assert "/mcp" in _content(rows[0])
        # Шапка: только #brand-block (MCP внутри карточки, не отдельным виджетом).
        trace = pilot.app.query_one("#trace")
        children = list(trace.children)
        assert children[0].id == "brand-block"


@pytest.mark.asyncio
async def test_indicator_all_ok_settles_to_ready(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.mcp_progress import McpProgress
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    _stub_factory(app)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        prog = McpProgress()
        prog.begin(["computer"])
        app._mcp_progress = prog
        scr._update_mcp_indicator()
        prog.update("computer", {"status": "connected"})
        scr._update_mcp_indicator()
        await pilot.pause()
        # все ✓ → done, sink погашен, блок с глифами ещё виден (лингер ~1с)
        assert scr._mcp_status_done is True
        assert app._mcp_progress is None
        assert scr._mcp_status_row is not None
        assert "готов к работе" not in _content(scr._mcp_status_row)
        # после лингера блок схлопывается в итог-запись (не удаляется)
        await pilot.pause(1.2)
        assert scr._mcp_status_row is not None
        assert scr._mcp_status_row.parent is not None
        body = _content(scr._mcp_status_row)
        assert "все подключены" in body
        assert "готов к работе" in body


@pytest.mark.asyncio
async def test_settle_card_reflects_mcp_fail_trace_clean(tmp_path):
    """После MCP-settle карточка несёт статус MCP + строку «⚠ N · /notices» (формат B,
    учтён упавший MCP) + одну «готов»; трейс чистый (нет блока «Уведомления»)."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.services.mcp_progress import McpProgress
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "mcp-servers": {"office": {}}})
    _stub_factory(app)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        trace = pilot.app.query_one("#trace")
        # mcp_status оседает к settle — эмулируем упавший office.
        app._mcp_status = {"office": {"status": "failed", "reason": "timeout"}}
        prog = McpProgress()
        prog.begin(["office"])
        app._mcp_progress = prog
        scr._update_mcp_indicator()
        prog.update("office", {"status": "failed", "reason": "timeout"})
        scr._update_mcp_indicator()
        await pilot.pause(1.2)   # лингер → settle
        card = _content(scr._mcp_status_row)
        # Точный формат карточки: три строки через пустую (MCP · уведомления · готовность).
        from tui.services.notices_ui import WARN_GLYPH
        assert card == (
            "⚠ MCP: 0/1 — ни один не подключился · /mcp\n\n"
            f"{WARN_GLYPH}1 · F4/notices\n\n"
            "✓ готов к работе"
        ), f"Формат карточки разошёлся: {card!r}"
        # Ровно одна «готов» во всём трейсе (готовность только в карточке).
        texts = [str(s.render()) for s in trace.query(Static)]
        ready_lines = [t for t in texts if "готов" in t]
        assert len(ready_lines) == 1, f"Ожидалась одна строка готовности: {ready_lines}"
        # Трейс чистый: блока-заголовка «Уведомления · N» нет.
        assert not any("Уведомления ·" in t for t in texts)
