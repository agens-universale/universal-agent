"""TDD Step 4: proxy field in ConnectionEditor."""
from __future__ import annotations

import pytest

pytest.importorskip("textual")

from gc_tui.editors.connection import ConnectionEditor
from textual.app import App, ComposeResult
from textual.widgets import Input


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


async def test_proxy_renders_in_compose():
    """Proxy input mounts when proxy_current is non-empty."""
    ed = ConnectionEditor(url_current="", auth_header=None, proxy_current="http://localhost:8080")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ids = [w.id for w in ed.query("*") if hasattr(w, "id") and w.id]
        assert "conn-proxy" in ids, f"expected #conn-proxy in {ids}"


async def test_proxy_renders_empty_when_not_set():
    """Proxy input mounts even when empty (placeholder shown)."""
    ed = ConnectionEditor(url_current="", auth_header=None, proxy_current="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ids = [w.id for w in ed.query("*") if hasattr(w, "id") and w.id]
        assert "conn-proxy" in ids


async def test_proxy_value_set_in_compose():
    """Proxy input pre-filled with proxy_current value."""
    ed = ConnectionEditor(url_current="", auth_header=None, proxy_current="socks5h://localhost:1080")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        proxy_input = ed.query_one("#conn-proxy", Input)
        assert proxy_input.value == "socks5h://localhost:1080"


async def test_proxy_in_peek():
    """peek() returns proxy field value as 4th element."""
    ed = ConnectionEditor(url_current="http://x", auth_header=None, proxy_current="http://p:8080")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-proxy", Input).value = "socks5h://host:999"
        url, auth_idx, token, proxy = ed.peek()
        assert proxy == "socks5h://host:999"


async def test_proxy_submit_passes_through():
    """submit() returns proxy as 4th element; None=unchanged, str=value (""=clear)."""
    ed = ConnectionEditor(url_current="http://x", auth_header=None, proxy_current="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-proxy", Input).value = "http://proxy:3128"
        url, header, error, proxy = ed.submit()
        assert proxy == "http://proxy:3128"
        assert error is None


async def test_proxy_submit_empty_unchanged_returns_none():
    """Пустое поле при пустом initial → proxy=None (не менять)."""
    ed = ConnectionEditor(url_current="http://x", auth_header=None, proxy_current="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        url, header, error, proxy = ed.submit()
        assert proxy is None


async def test_proxy_submit_clear_returns_empty_string():
    """Очистка ранее заданного proxy → "" («не задано» → env)."""
    ed = ConnectionEditor(
        url_current="http://x",
        auth_header=None,
        proxy_current="http://old:8080",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-proxy", Input).value = ""
        url, header, error, proxy = ed.submit()
        assert proxy == ""
        assert error is None


async def test_proxy_submit_unchanged_returns_none():
    """Неизменённый proxy → None (не писать в файл)."""
    ed = ConnectionEditor(
        url_current="http://x",
        auth_header=None,
        proxy_current="http://px:8080",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        url, header, error, proxy = ed.submit()
        assert proxy is None
        assert error is None


async def test_proxy_dirty_when_changed():
    """Changing proxy field makes editor dirty."""
    ed = ConnectionEditor(url_current="http://x", auth_header=None, proxy_current="http://old:80")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-proxy", Input).value = "http://new:8080"
        assert ed.is_dirty() is True


async def test_proxy_not_dirty_when_unchanged():
    """No change to proxy → not dirty."""
    ed = ConnectionEditor(url_current="http://x", auth_header=None, proxy_current="http://px:8080")
    assert ed.is_dirty() is False


async def test_navigation_includes_proxy():
    """move() cycles through all positions; proxy is reachable."""
    ed = ConnectionEditor(url_current="http://x", auth_header=None, proxy_current="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        # move() cycles 4 positions: 0(url), 1(token), 2(no-auth), 3(proxy)
        for _ in range(4):
            ed.move(1)
        # After 4 moves from 0 → back at 0
        assert ed._idx == 0


async def test_proxy_submit_error_still_returns_proxy():
    """Even when submit() returns an error (e.g. bad URL), proxy is still returned."""
    ed = ConnectionEditor(url_current="", auth_header=None, proxy_current="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-proxy", Input).value = "http://p:8080"
        # URL is empty → submit returns error
        url, header, error, proxy = ed.submit()
        assert error == "введите адрес"
        assert proxy == "http://p:8080"  # proxy still preserved
