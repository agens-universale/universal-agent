"""FastMCP server factory.

Registers tools based on ServerConfig:
- readonly=True -> only get_state, wait (+ vision if enabled)
- enable_vision=True -> adds capture_screenshot, analyze_screen_region
- disabled_tools -> removes by name
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import ServerConfig
from .core.state_store import StateStore, cleanup_stale
from .errors import ComputerMCPError, ErrorCategory
from .guards import safe_tool, timeout_guard
from .tools import keyboard as kb_tools
from .tools import mouse as mouse_tools
from .tools import state as state_tools
from .tools import vision as vision_tools
from .tools import windows as win_tools


def _make_provider():
    if sys.platform != "win32":
        raise ComputerMCPError(
            ErrorCategory.UIA_UNAVAILABLE,
            "gc-computer-mcp runs only on Windows",
        )
    from .core.uia_windows import WindowsUIAProvider

    return WindowsUIAProvider()


def build_server(cfg: ServerConfig) -> FastMCP:
    cleanup_stale(cfg.state_ttl_seconds)
    mcp = FastMCP("gc-computer-mcp")

    # Lazy provider init — deferred until first tool call so test_server_wiring
    # can instantiate the server on non-Windows CI without raising.
    provider_cache: dict[str, Any] = {}

    def get_provider():
        if "p" not in provider_cache:
            provider_cache["p"] = _make_provider()
        return provider_cache["p"]

    store = StateStore(
        session_id=cfg.session_id,
        ttl_seconds=cfg.state_ttl_seconds,
    )

    disabled = cfg.disabled_tools

    def register(name: str, fn: Callable, *, mutating: bool) -> None:
        if name in disabled:
            return
        if cfg.readonly and mutating:
            return
        wrapped = safe_tool()(timeout_guard(seconds=cfg.timeouts.get(name, 5.0))(fn))
        mcp.tool(name=name)(wrapped)

    # --- read-only tools (always available unless explicitly disabled)
    def _get_state(
        scope: str = "foreground",
        window_title: str | None = None,
        max_depth: int | None = None,
        include_invisible: bool = False,
        exclude_shell: bool = True,
        only_interactive: bool = False,
    ) -> dict:
        """Capture current UI state (scoped, token-efficient).

        Returns a text rendering of the UIA tree with numeric element_ids,
        plus a list of top-level windows. Element ids are valid only until
        the next state-changing action. Call this before any click/type.

        Args:
            scope: 'foreground' (default, current window only, token-efficient)
                / 'window' (needs window_title) / 'desktop' (full tree, large).
            window_title: substring of target window title (for scope='window').
            max_depth: if set, drop elements deeper than N levels from scope root.
            include_invisible: include offscreen/hidden elements (rarely useful).
            exclude_shell: for scope='desktop', drop taskbar & Program Manager.
            only_interactive: keep only clickable/editable controls + ancestors.
        """
        return state_tools.get_state(
            provider=get_provider(),
            store=store,
            scope=scope,
            window_title=window_title,
            max_depth=max_depth,
            include_invisible=include_invisible,
            exclude_shell=exclude_shell,
            only_interactive=only_interactive,
        )

    def _wait(seconds: float) -> dict:
        """Sleep for the given number of seconds (0..30)."""
        return state_tools.wait(seconds=seconds)

    register("get_state", _get_state, mutating=False)
    register("wait", _wait, mutating=False)

    # --- mouse tools
    def _click(element_id: int, button: str = "left", clicks: int = 1) -> dict:
        """Click a UIA element by its id from the last get_state."""
        return mouse_tools.click(
            element_id=element_id, button=button, clicks=clicks,
            provider=get_provider(), store=store,
        )

    def _scroll(direction: str, amount: int = 3, element_id: int | None = None) -> dict:
        """Scroll the active area or a specific element."""
        return mouse_tools.scroll(
            element_id=element_id, direction=direction, amount=amount,
            provider=get_provider(), store=store,
        )

    def _drag(from_element_id: int, to_element_id: int, button: str = "left") -> dict:
        """Drag one element onto another."""
        return mouse_tools.drag(
            from_element_id=from_element_id, to_element_id=to_element_id,
            button=button, provider=get_provider(), store=store,
        )

    register("click", _click, mutating=True)
    register("scroll", _scroll, mutating=True)
    register("drag", _drag, mutating=True)

    # --- keyboard tools
    def _type(element_id: int, text: str, clear: bool = False,
              press_enter: bool = False) -> dict:
        """Type text into a text field."""
        return kb_tools.type_text(
            element_id=element_id, text=text, clear=clear,
            press_enter=press_enter,
            provider=get_provider(), store=store,
        )

    def _key(key: str) -> dict:
        """Press a single key: 'enter', 'escape', 'tab', 'f5', etc."""
        return kb_tools.key(key=key)

    def _shortcut(keys: list[str]) -> dict:
        """Press a key combination, e.g. ['ctrl', 's']. Dangerous combos blocked."""
        return kb_tools.shortcut(keys=keys)

    register("type", _type, mutating=True)
    register("key", _key, mutating=True)
    register("shortcut", _shortcut, mutating=True)

    # --- window tools
    def _resize(window_title: str, x: int | None = None, y: int | None = None,
                width: int | None = None, height: int | None = None,
                maximize: bool = False, minimize: bool = False) -> dict:
        """Resize/move/minimize/maximize a window by title substring."""
        return win_tools.resize(
            window_title=window_title, x=x, y=y, width=width, height=height,
            maximize=maximize, minimize=minimize,
        )

    def _switch(window_title: str) -> dict:
        """Bring window (matched by title substring) to the foreground."""
        return win_tools.switch(window_title=window_title)

    def _launch_app(app_name: str) -> dict:
        """Launch an application by friendly name ('excel', 'word', 'notepad', 'Jazz', ...).

        Returns launched=<name>, window_title=<detected title or null>,
        window_hwnd=<HWND or null>. Use window_title in your next get_state
        call with scope='window' to skip a full desktop snapshot.
        """
        return win_tools.launch_app(app_name=app_name)

    register("resize", _resize, mutating=True)
    register("switch", _switch, mutating=True)
    register("launch_app", _launch_app, mutating=True)

    # --- vision (optional)
    if cfg.enable_vision:
        def _capture_screenshot(region: dict | None = None,
                                downscale: bool = False) -> dict:
            """Capture a PNG of screen or region (vision-model use)."""
            return vision_tools.capture_screenshot(region=region, downscale=downscale)

        def _analyze_screen_region(x: int, y: int, width: int, height: int) -> dict:
            """Capture a PNG of a specific screen region for focused analysis."""
            return vision_tools.analyze_screen_region(
                x=x, y=y, width=width, height=height,
            )

        # vision tools are not "mutating" per se but they are opt-in:
        register("capture_screenshot", _capture_screenshot, mutating=False)
        register("analyze_screen_region", _analyze_screen_region, mutating=False)

    return mcp
