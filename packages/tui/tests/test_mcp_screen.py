from gc_testkit.tui_support import as_settings, tui_settings
import pytest


def _yaml_of(screen):
    return screen.rendered_text()


def test_mcp_cards_text_shows_status_reason_tools():
    from gc_tui.screens.mcp import McpScreen

    snap = {
        "office": {
            "status": "connected",
            "reason": None,
            "description": "Read office docs",
            "tools": [{"name": "mcp_office_read", "description": "d"}],
        },
        "computer": {
            "status": "failed",
            "reason": "команда 'computer-mcp' не найдена",
            "description": "GUI control",
            "tools": [],
        },
    }
    scr = McpScreen(tools_snapshot=snap)
    text = scr.rendered_text()
    assert "office" in text and "connected" in text
    assert "Read office docs" in text
    assert "mcp_office_read" not in text  # свёрнуто: имён тулов нет
    assert "computer" in text and "failed" in text
    assert "команда 'computer-mcp' не найдена" in text
    assert "1 connected" in text and "1 failed" in text
    scr.cards[0].toggle()  # раскрыли office
    assert (
        "read" in scr.rendered_text()
    )  # короткое имя (префикс mcp_office_ срезан)


def test_mcp_cards_empty_when_no_snapshot():
    from gc_tui.screens.mcp import McpScreen

    scr = McpScreen(tools_snapshot=None)
    assert "после первого запуска" in scr.rendered_text()


@pytest.mark.asyncio
async def test_mcp_screen_with_snapshot(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.mcp import McpScreen

    cfg = {
        "mcp.servers": {
            "search": {"transport": "sse", "url": "http://x", "token": "T"}
        }
    }
    snap = {
        "search": {
            "status": "connected",
            "reason": None,
            "description": "web search",
            "tools": [{"name": "search.web", "description": "web"}],
        }
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test() as pilot:
        scr = McpScreen(settings=as_settings(cfg), tools_snapshot=snap)
        await app.push_screen(scr)
        await pilot.pause()
        t = _yaml_of(scr)
        assert "search" in t and "connected" in t
        assert "search.web" not in t  # свёрнуто
        scr.cards[0].toggle()
        await pilot.pause()
        assert "search.web" in _yaml_of(
            scr
        )  # имя без префикса mcp_ остаётся как есть


@pytest.mark.asyncio
async def test_mcp_screen_no_snapshot_shows_note(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.mcp import McpScreen

    cfg = {"mcp.servers": {"search": {"transport": "sse", "url": "http://x"}}}
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test() as pilot:
        scr = McpScreen(settings=as_settings(cfg), tools_snapshot=None)
        await app.push_screen(scr)
        await pilot.pause()
        t = _yaml_of(scr)
        assert "после первого запуска" in t


@pytest.mark.asyncio
async def test_mcp_screen_empty(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.mcp import McpScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"mcp.servers": {}}),
    )
    async with app.run_test() as pilot:
        scr = McpScreen(
            settings=as_settings({"mcp.servers": {}}), tools_snapshot={}
        )
        await app.push_screen(scr)
        await pilot.pause()
        assert "нет MCP-серверов" in _yaml_of(scr)


@pytest.mark.asyncio
async def test_mcp_screen_esc_returns_to_main(tmp_path):
    # Esc должен возвращать на главный экран, а не выходить из TUI.
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"mcp.servers": {}}),
    )
    async with app.run_test() as pilot:
        await pilot.app.screen._handle_command("mcp")
        await pilot.pause()
        from gc_tui.screens.mcp import McpScreen

        assert isinstance(app.screen, McpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, TraceScreen)


@pytest.mark.asyncio
async def test_mcp_screen_has_header_and_footer(tmp_path):
    # Консистентность с прочими экранами: есть #header и #footer.
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.mcp import McpScreen

    cfg = {"mcp.servers": {"search": {"transport": "sse", "url": "http://x"}}}
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test() as pilot:
        scr = McpScreen(settings=as_settings(cfg), tools_snapshot=None)
        await app.push_screen(scr)
        await pilot.pause()
        assert scr.query("#header") and scr.query("#footer")


def test_mcp_card_reason_on_failed_line():
    from gc_tui.screens.mcp import McpScreen

    snap = {
        "computer": {
            "status": "failed",
            "reason": "команда не найдена",
            "description": "GUI",
            "tools": [],
        }
    }
    text = McpScreen(tools_snapshot=snap).rendered_text()
    failed_line = next(ln for ln in text.splitlines() if "failed" in ln)
    assert (
        "команда не найдена" in failed_line
    )  # причина в той же строке, что failed
    assert "причина:" not in text  # старого отдельного «причина:» больше нет


def test_mcp_renderable_is_rich_text_with_styles():
    from gc_tui.screens.mcp import McpScreen
    from rich.text import Text

    snap = {
        "office": {
            "status": "connected",
            "reason": None,
            "description": "d",
            "tools": [{"name": "mcp_office_x", "description": "y"}],
        }
    }
    rend = McpScreen(tools_snapshot=snap).cards[0]._renderable()
    assert isinstance(rend, Text)
    assert len(rend.spans) > 0  # есть цветовые сегменты


# ---- ServerCard: свёрнутый/раскрытый рендер (spec 2026-06-10) ----


def _card(name, info):
    from gc_tui.screens.mcp import ServerCard

    return ServerCard(name, info)


def test_card_collapsed_shows_marker_glyph_and_desc_but_no_tools():
    card = _card(
        "office",
        {
            "status": "connected",
            "reason": None,
            "description": "Read office docs",
            "tools": [
                {
                    "name": "mcp_office_read_file",
                    "description": "Читает документ",
                }
            ],
        },
    )
    text = card._renderable().plain
    assert text.startswith("▸ ✓ office")  # маркер + статус-глиф + имя
    assert "connected · 1 тулов" in text
    assert "· Read office docs" in text  # описание видно всегда
    assert "read_file" not in text  # тулы скрыты в свёрнутом виде


def test_card_expanded_lists_tools_with_stripped_prefix_and_description():
    card = _card(
        "office",
        {
            "status": "connected",
            "reason": None,
            "description": "d",
            "tools": [
                {
                    "name": "mcp_office_read_file",
                    "description": "Читает документ",
                },
                {
                    "name": "mcp_office_create_docx",
                    "description": "Создаёт .docx",
                },
            ],
        },
    )
    card.toggle()
    text = card._renderable().plain
    assert text.startswith("▾ ✓ office")
    assert "mcp_office_" not in text  # префикс срезан
    assert "read_file" in text and "Читает документ" in text
    assert "create_docx" in text and "Создаёт .docx" in text
    # колонка имён выровнена: описание начинается на одной позиции
    lines = [
        ln for ln in text.splitlines() if "Читает" in ln or "Создаёт" in ln
    ]
    assert lines[0].index("Читает") == lines[1].index("Создаёт")


def test_card_tool_name_without_prefix_kept_as_is():
    card = _card(
        "search",
        {
            "status": "connected",
            "reason": None,
            "description": "",
            "tools": [{"name": "search.web", "description": "web"}],
        },
    )
    card.toggle()
    assert "search.web" in card._renderable().plain


def test_card_failed_collapsed_truncates_reason_expanded_shows_full():
    long_reason = "строка-" * 30  # > 90 символов
    card = _card(
        "computer",
        {
            "status": "failed",
            "reason": long_reason,
            "description": "GUI control",
            "tools": [],
        },
    )
    collapsed = card._renderable().plain
    assert collapsed.startswith("▸ ✗ computer")
    assert long_reason not in collapsed  # в шапке причина обрезана
    card.toggle()
    expanded = card._renderable().plain
    assert long_reason in expanded  # раскрытие — полная причина


def test_card_toggle_flips_expanded_and_marker():
    card = _card(
        "office",
        {
            "status": "connected",
            "reason": None,
            "description": "",
            "tools": [],
        },
    )
    assert card.expanded is False
    card.toggle()
    assert card.expanded is True
    assert card._renderable().plain.startswith("▾")
    card.toggle()
    assert card.expanded is False
    assert card._renderable().plain.startswith("▸")
