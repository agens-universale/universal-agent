"""Отказы слота футера — цветом WARN, подсказки и советы — цветом
подсказки (решение владельца 10 к плану P3b)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from gc_testkit.tui_support import tui_settings

from gc_tui.services.footer_slot import (
    IMAGE_OPEN_TIP,
    IMAGE_PASTE_REFUSALS,
    IMAGE_REFUSAL_EMPTY_CLIPBOARD,
    IMAGES_WITH_COMMAND_REFUSAL,
    NESTED_NO_DETAIL,
    QUEUE_ADMITTED_HINT,
    RETENTION_DETAIL_HINT,
)

pytest.importorskip("textual")


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _open_image_refusal(scr, monkeypatch):
    from gc_tui.screens import main

    monkeypatch.setattr(main, "open_image", lambda path: "✗ не открыть")
    scr.on_open_image_request(SimpleNamespace(path="x.png"))


def _profile_not_found(scr, monkeypatch):
    from gc_tui import toggles

    monkeypatch.setattr(toggles, "effective_toggle_names", lambda a, n: [])
    scr._cmd_profiles("нет-такого")


def _toggle_unavailable(scr, monkeypatch):
    from gc_tui.screens import main

    monkeypatch.setattr(
        main, "toggle_unavailable_hint", lambda app, n, v: "недоступно"
    )
    scr._toggle_info_command("level", "auto")


def _nested_no_detail(scr, monkeypatch):
    scr._nested_tool_rows["c1"] = object()
    monkeypatch.setattr(
        scr, "_resolve_nested_detail_events", lambda cid: (None, None)
    )
    scr.on_open_tool_call_detail_request(SimpleNamespace(call_id="c1"))


def _retention_detail(scr, monkeypatch):
    from gc_tui.screens import main

    scr._task_events.append(SimpleNamespace(call_id="c1", kind="call"))
    monkeypatch.setattr(main, "is_light_payload", lambda ev: True)
    scr.on_open_tool_call_detail_request(SimpleNamespace(call_id="c1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "show",
    [
        _open_image_refusal,
        _profile_not_found,
        _toggle_unavailable,
        _nested_no_detail,
        _retention_detail,
    ],
)
async def test_refusal_paths_are_warn(tmp_path, monkeypatch, show):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        show(scr, monkeypatch)
        assert scr._footer_hint_active
        assert scr._footer_hint_tone == "warn"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        QUEUE_ADMITTED_HINT,
        IMAGES_WITH_COMMAND_REFUSAL,
        IMAGE_REFUSAL_EMPTY_CLIPBOARD,
        NESTED_NO_DETAIL,
        RETENTION_DETAIL_HINT,
        "✗ не сохранено: причина",
        *IMAGE_PASTE_REFUSALS.values(),
    ],
)
async def test_refusal_text_is_warn(tmp_path, text):
    from gc_tui.palette import WARN

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._show_footer_refusal(text)
        spans = scr._footer_hint_body().spans
        assert [(sp.start, sp.end, sp.style) for sp in spans] == [
            (0, len(text), WARN)
        ]


@pytest.mark.asyncio
async def test_tip_and_info_are_not_warn(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._show_footer_tip(IMAGE_OPEN_TIP)
        assert scr._footer_hint_tone is None
        scr._toggle_info_command("level")
        assert scr._footer_hint_tone is None
