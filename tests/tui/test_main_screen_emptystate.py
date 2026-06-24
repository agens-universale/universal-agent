"""Brand-блок: постоянная шапка #trace — виден в empty-state и после событий."""
import pytest


@pytest.mark.asyncio
async def test_brand_block_visible_when_no_task():
    pytest.importorskip("textual")
    from tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        logos = pilot.app.query(".brand-logo")
        assert len(logos) > 0
        from tui import branding
        assert str(logos[0].render()) == branding.LOGO


@pytest.mark.asyncio
async def test_brand_block_persists_after_first_event():
    """Claude-style: бренд не исчезает на первом событии — остаётся шапкой трейса."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._mount_trace_row(prefix="> ", content="привет",
                             kind_class="line-user-mark")
        await pilot.pause()
        logos = pilot.app.query(".brand-logo")
        assert len(logos) > 0
        # Бренд — первый ребёнок #trace, событие смонтировано ПОД ним.
        trace = pilot.app.query_one("#trace")
        children = list(trace.children)
        assert children[0].id == "brand-block"
        assert len(children) >= 2


@pytest.mark.asyncio
async def test_footer_switches_to_main_text_on_attach_active_task(tmp_path):
    """Footer-left changes from empty-state text to main-screen text after attach_active_task."""
    pytest.importorskip("textual")
    import json
    from pathlib import Path
    from tui.app import TuiApp

    tid = "t1"
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "...", "messages_count": 0}),
        encoding="utf-8",
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})

    async with app.run_test() as pilot:
        # Initial: empty-state — без [Esc] back/выход, без /stop, без /export.
        footer_left = pilot.app.query_one("#footer-left")
        rendered = str(footer_left.render())
        assert "/help" in rendered
        assert "/sessions" in rendered
        assert "/config" in rendered
        assert "/mcp" in rendered
        assert "/skills" not in rendered
        assert "/stop" not in rendered
        assert "/export" not in rendered
        assert "Esc" not in rendered
        assert "выход" not in rendered
        footer_right = pilot.app.query_one("#footer-right")
        # footer-right показывает модель·контекст (gc-ua удалено в Task 3).
        assert "gc-ua" not in str(footer_right.render())
        assert "M" in str(footer_right.render())

        # Attach task → idle (нет active_runner) → тот же набор команд, /stop НЕТ.
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause()

        footer_left = pilot.app.query_one("#footer-left")
        rendered = str(footer_left.render())
        assert "/sessions" in rendered
        assert "/help" in rendered
        assert "/config" in rendered
        assert "/mcp" in rendered
        assert "/skills" not in rendered
        assert "/export" not in rendered
        assert "/stop" not in rendered
        assert "Esc" not in rendered
        assert "back" not in rendered
        # footer-right показывает модель (gc-ua удалено в Task 3).
        footer_right = pilot.app.query_one("#footer-right")
        assert "gc-ua" not in str(footer_right.render())
        assert "M" in str(footer_right.render())


@pytest.mark.asyncio
async def test_brand_block_survives_switch_to_empty_state():
    """/new: пересборка трейса щадит бренд (remove_children асинхронный — ремоунт с тем же id невозможен)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._mount_trace_row(prefix="> ", content="привет",
                             kind_class="line-user-mark")
        await pilot.pause()
        scr._switch_to_empty_state()
        await pilot.pause()
        logos = pilot.app.query(".brand-logo")
        assert len(logos) > 0
        trace = pilot.app.query_one("#trace")
        children = list(trace.children)
        assert children and children[0].id == "brand-block"
        assert len(children) == 1  # контент удалён, бренд остался


@pytest.mark.asyncio
async def test_brand_block_shown_on_attach_existing_session(tmp_path):
    """Resume сессии с историей: бренд — первый ребёнок #trace, история под ним."""
    pytest.importorskip("textual")
    import json
    from pathlib import Path
    from tui.app import TuiApp

    tid = "t-resume"
    data = Path(tmp_path) / f"data_cli_{tid}.jsonl"
    data.write_text(json.dumps({
        "type": "message", "seq": 1,
        "payload": {"id": "m1", "role": "user", "content": "старое сообщение"},
    }) + "\n", encoding="utf-8")
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps({
        "thread_id": tid, "system_id": "cli",
        "updated_at": "2026-06-10T10:00:00+00:00", "messages_count": 1,
    }), encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause()
        trace = pilot.app.query_one("#trace")
        children = list(trace.children)
        assert children and children[0].id == "brand-block"
        text = "\n".join(str(c.render()) for c in trace.walk_children())
        assert "старое сообщение" in text
