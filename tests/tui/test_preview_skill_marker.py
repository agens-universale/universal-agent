"""Превью сессии (Ctrl+P) — [skill]-маркер в read-only рендере.

Паритет с MainScreen._render_record: skill_loaded из payload монтирует
[skill]-маркер как для skill+prompt сообщений, так и для mid-run delivery
(display_content="", ev=None). Тест намеренно пишется ДО реализации (TDD).
"""
import pytest


def _rec(payload, ts="2026-06-01T12:00:00Z"):
    """Envelope-запись вокруг payload."""
    return {"type": "message", "thread_id": "t", "system": "cli", "seq": 1,
            "ts": ts, "payload": payload}


@pytest.mark.asyncio
async def test_skill_plus_prompt_shows_marker_and_reply():
    """skill+prompt: display_content непустой → map_message → UserReplyContainer +
    маркер [skill] после реплики."""
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll
    from tui.services.trace_render import render_thread_readonly
    from tui.widgets.trace_row import TraceRow
    from tui.widgets.user_reply import UserReplyContainer

    records = [_rec({
        "role": "user",
        "content": "<skill>\nдавай",
        "display_content": "давай",
        "skill_loaded": "qa-x",
        "id": "m1",
    })]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_thread_readonly(self.query_one("#t", VerticalScroll), records)

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        # Реплика пользователя смонтирована
        assert len(scr.query(UserReplyContainer)) == 1
        # Ровно один [skill]-маркер
        markers = [w for w in scr.query(TraceRow) if w.has_class("skill-marker")]
        assert len(markers) == 1, f"Ожидался 1 skill-маркер, найдено {len(markers)}"
        # Содержимое маркера: «`qa-x` загружен»
        assert "`qa-x` загружен" in markers[0]._content


@pytest.mark.asyncio
async def test_mid_run_delivery_shows_only_marker():
    """mid-run delivery: display_content="" → map_message возвращает None.
    Только [skill]-маркер; UserReplyContainer НЕ монтируется."""
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll
    from tui.services.trace_render import render_thread_readonly
    from tui.widgets.trace_row import TraceRow
    from tui.widgets.user_reply import UserReplyContainer

    records = [_rec({
        "role": "user",
        "content": "<skill>",
        "display_content": "",
        "skill_loaded": "qa-x",
        "id": "m2",
    })]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_thread_readonly(self.query_one("#t", VerticalScroll), records)

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        # UserReplyContainer НЕ монтируется (display_content пустой)
        assert len(scr.query(UserReplyContainer)) == 0
        # Ровно один [skill]-маркер
        markers = [w for w in scr.query(TraceRow) if w.has_class("skill-marker")]
        assert len(markers) == 1, f"Ожидался 1 skill-маркер, найдено {len(markers)}"
        assert "`qa-x` загружен" in markers[0]._content


@pytest.mark.asyncio
async def test_normal_user_record_no_marker():
    """Обычная реплика без skill_loaded → [skill]-маркер НЕ монтируется."""
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll
    from tui.services.trace_render import render_thread_readonly
    from tui.widgets.trace_row import TraceRow
    from tui.widgets.user_reply import UserReplyContainer

    records = [_rec({
        "role": "user",
        "content": "привет",
        "id": "m3",
    })]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_thread_readonly(self.query_one("#t", VerticalScroll), records)

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        # Реплика смонтирована
        assert len(scr.query(UserReplyContainer)) == 1
        # Маркера нет
        markers = [w for w in scr.query(TraceRow) if w.has_class("skill-marker")]
        assert len(markers) == 0, f"Маркеров быть не должно, найдено {len(markers)}"
