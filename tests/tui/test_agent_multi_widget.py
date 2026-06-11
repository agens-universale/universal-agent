"""Agent reply с fenced-кодом → multi-widget mount (prose + code-block-panel)."""
import json
from pathlib import Path

import pytest


def _write_thread_with_agent_reply(tmp_path, tid, content):
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "status": "stopped",
                    "updated_at": "2026-05-26T12:00:00+00:00", "messages_count": 2}),
        encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "2026-05-26T12:00:00+00:00",
                    "payload": {"id": "u1", "role": "user", "content": "вопрос"}}) + "\n"
        + json.dumps({"type": "message", "seq": 2, "ts_utc": "2026-05-26T12:00:01+00:00",
                      "payload": {"id": "a1", "role": "assistant", "content": content}}) + "\n",
        encoding="utf-8")


@pytest.mark.asyncio
async def test_prose_only_reply_renders_one_static_with_bullet(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import TraceRow

    tid = "prose_only"
    _write_thread_with_agent_reply(tmp_path, tid, "Привет, я помогу.")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        scr = pilot.app.screen
        agent_rows = [
            r for r in scr.query(TraceRow)
            if r.has_class("line-agent")
        ]
        # Один prose-блок → один TraceRow; первый имеет prefix '● '.
        assert len(agent_rows) == 1
        assert agent_rows[0]._prefix == "● "


@pytest.mark.asyncio
async def test_reply_with_fenced_block_mounts_separate_panels(tmp_path):
    """prose + fenced + prose → 3 виджета: 2 prose-TraceRow + 1 code-block-panel."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import TraceRow

    tid = "with_code"
    content = "Вот пример:\n```python\nx = 1\n```\nИ всё."
    _write_thread_with_agent_reply(tmp_path, tid, content)

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        scr = pilot.app.screen
        prose_rows = [
            r for r in scr.query(TraceRow)
            if r.has_class("line-agent")
        ]
        code_panels = scr.query(".code-block-panel")
        # 2 prose-TraceRow + 1 code-panel.
        assert len(prose_rows) == 2
        assert len(code_panels) == 1


@pytest.mark.asyncio
async def test_continuation_widgets_have_cont_class(tmp_path):
    """Второй и далее prose-блоки в одной agent-реплике получают .line-agent-cont."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import TraceRow

    tid = "multi_para"
    # Три параграфа prose — каждый отдельный блок.
    content = "Первый параграф.\n\nВторой параграф.\n\nТретий параграф."
    _write_thread_with_agent_reply(tmp_path, tid, content)

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        scr = pilot.app.screen
        # TraceRow'ы prose-блоков в порядке появления в DOM.
        prose_rows = [
            r for r in scr.query(TraceRow)
            if r.has_class("line-agent")
        ]
        assert len(prose_rows) == 3
        # Первый: line-agent без line-agent-cont.
        assert not prose_rows[0].has_class("line-agent-cont")
        # Второй и третий: имеют line-agent-cont.
        assert prose_rows[1].has_class("line-agent-cont")
        assert prose_rows[2].has_class("line-agent-cont")


@pytest.mark.asyncio
async def test_unknown_lang_falls_back_to_code_block_color(tmp_path):
    """fenced с неизвестным языком → panel остаётся, текст моноцветный (не падает)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    tid = "unknown_lang"
    content = "Код:\n```xyz_bogus_lang\nsome content here\n```"
    _write_thread_with_agent_reply(tmp_path, tid, content)

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        scr = pilot.app.screen
        # Panel смонтирован, контент рендерится без exception.
        code_panels = scr.query(".code-block-panel")
        assert len(code_panels) == 1
        rendered = str(code_panels[0].render())
        assert "some content here" in rendered
