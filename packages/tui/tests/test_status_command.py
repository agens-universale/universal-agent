"""'/status' дампит meta + runtime активной задачи в Viewer."""

from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest


def _viewer_body(screen) -> str:
    """Достать весь рендеримый текст из ViewerScreen.
    body теперь через render_payload → Static(Syntax(json)), и `str(render())`
    даёт `RichVisual(...)` вместо контента. Извлекаем `.content.code`/`.plain` напрямую."""
    from textual.widgets import Static

    parts = []
    for s in screen.query(Static):
        content = getattr(s, "content", None) or getattr(s, "renderable", None)
        if content is None:
            parts.append(str(s.render()))
            continue
        if hasattr(content, "code"):  # rich.syntax.Syntax
            parts.append(content.code)
        elif hasattr(content, "plain"):  # rich.text.Text
            parts.append(content.plain)
        else:
            parts.append(str(content))
    return "\n".join(parts)


def _write_task(tmp_path, tid):
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-22T12:00:00+00:00",
                "messages_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts_utc": "2026-05-22T12:00:00+00:00",
                "payload": {"id": "u1", "role": "user", "content": "x"},
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_status_with_active_task_opens_viewer_with_meta_and_runtime(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    tid = "st1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.2)

        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/status"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ViewerScreen)
        body = _viewer_body(pilot.app.screen)
        assert '"meta"' in body
        assert '"runtime"' in body
        assert tid in body  # активная задача в meta
        assert '"runner_is_running"' in body


@pytest.mark.asyncio
async def test_status_without_active_task_shows_idle(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/status"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ViewerScreen)
        body = _viewer_body(pilot.app.screen)
        assert '"meta": null' in body
        assert '"active_task_id": null' in body


@pytest.mark.asyncio
async def test_status_shows_auto_level_literal(tmp_path):
    """model: auto (спека auto-routing §5.1) — /status показывает режим
    (при доступном автовыборе — `auto`, правило палитры `toggle_state`), не
    резолвленный уровень такта: витрина отвечает на «в каком режиме сессия»,
    футеру нужен факт хода."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    cfg = {
        "agent.model": "m",
        "model": "auto",
        "backend.api_mode": "chat_completions",
        "agent.auto_routing": True,
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/status"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ViewerScreen)
        body = _viewer_body(pilot.app.screen)
        assert '"level": "auto"' in body


@pytest.mark.asyncio
async def test_status_shows_only_two_level_in_auto_mode(tmp_path):
    """При доступном level: auto витрина называет единственную политику two_level."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    cfg = {
        "agent.model": "m",
        "model": "auto",
        "backend.api_mode": "chat_completions",
        "agent.auto_routing": True,
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/status"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert isinstance(pilot.app.screen, ViewerScreen)
        assert '"routing_policy": "two_level"' in _viewer_body(
            pilot.app.screen
        )


@pytest.mark.asyncio
async def test_status_leaves_routing_policy_empty_in_manual_mode(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {
        "agent.model": "m",
        "model": "default",
        "backend.api_mode": "chat_completions",
        "agent.auto_routing": True,
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/status"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert '"routing_policy": null' in _viewer_body(pilot.app.screen)


@pytest.mark.asyncio
async def test_status_dash_dash_raw_accepted_no_crash(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/status --raw"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, ViewerScreen)


@pytest.mark.parametrize(
    "extra",
    [
        {"agent.auto_routing": False},
        {"backend.api_mode": "responses"},
        {"models": {"only": {}}, "slots": {"default": "only"}},
    ],
    ids=["auto_routing_off", "responses", "one_model"],
)
@pytest.mark.asyncio
async def test_status_shows_the_working_level_when_auto_is_unavailable(
    tmp_path, extra
):
    """`level: auto`, а автовыбор недоступен: /status называет уровень, которым
    идёт работа (`default`, как палитра и футер), и не называет политику."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {
        "agent.model": "m",
        "model": "auto",
        "backend.api_mode": "chat_completions",
        "agent.auto_routing": True,
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
        **extra,
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/status"
        await pilot.press("enter")
        await pilot.pause(0.2)

        body = _viewer_body(pilot.app.screen)
        assert (
            '"level": "default"' in body and '"routing_policy": null' in body
        )
