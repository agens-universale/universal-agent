"""PreviewScreen — реальные виджеты трейса + «сессия <id>» слева, путь справа в шапке."""

import pytest


def _rec(payload):
    return {
        "type": "message",
        "thread_id": "t",
        "system": "cli",
        "seq": 1,
        "ts": "2026-05-30T10:00:00Z",
        "payload": payload,
    }


def _segs(records):
    return [{"index": 0, "messages": records, "compacted_by": None}]


@pytest.mark.asyncio
async def test_preview_renders_trace_widgets_and_header():
    pytest.importorskip("textual")
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.widgets.user_reply import UserReplyContainer
    from textual.app import App
    from textual.widgets import Static

    result = {}
    records = [_rec({"role": "user", "content": "вопрос-превью", "id": "m1"})]

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=_segs(records),
                    thread_id="abc12345xyz",
                    project_path="D:\\dev\\proj\\foo",
                ),
                lambda v: result.update(v=v),
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        assert (
            len(scr.query(UserReplyContainer)) == 1
        )  # реальный виджет трейса
        assert len(scr.query("#cmdinput")) == 0  # без поля ввода
        # шапка слева — «превью · сессия <id8>» (промпт сессии не дублируется)
        assert "сессия abc12345" in str(
            scr.query_one("#header-left", Static).render()
        )
        # путь проекта справа в шапке
        assert "foo" in str(scr.query_one("#header-right", Static).render())
        # футер — только подсказки, без правого блока
        assert len(scr.query("#preview-footer-right")) == 0
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] is True


def _sum(text):
    return f'<technical type="dialog_summary"><summary>{text}</summary></technical>'


@pytest.mark.asyncio
async def test_preview_summary_marker_collapsed_then_toggles():
    """Маркер сжатия в превью свёрнут по умолчанию; клик раскрывает тело."""
    pytest.importorskip("textual")
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.widgets.trace_row import SummaryMarker
    from textual.app import App

    # seg0 [вопрос] сжат → СИНТЕЗ; seg1 (live) [ответ]
    segments = [
        {
            "index": 0,
            "messages": [
                _rec({"role": "user", "content": "вопрос", "id": "m0"})
            ],
            "compacted_by": {
                "ts": "t",
                "messages_in_segment": 1,
                "synthesis": "СИНТЕЗ-ПРЕВЬЮ",
            },
        },
        {
            "index": 1,
            "messages": [
                _rec({"role": "user", "content": "ответ", "id": "m1"})
            ],
            "compacted_by": None,
        },
    ]

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=segments, thread_id="t", project_path="p"
                )
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        body = next(
            w for w in scr.query(SummaryMarker) if w.has_class("summary-body")
        )
        marker = next(
            w
            for w in scr.query(SummaryMarker)
            if w.has_class("summary-marker")
        )
        prefix = lambda: marker._content.plain
        # свёрнут по умолчанию — стрелка вправо
        assert body.has_class("hidden")
        assert "▸" in prefix() and "▾" not in prefix()

        # клик по маркеру → раскрыт, стрелка вниз
        class _Click:
            chain = 1

            def stop(self):
                pass

        marker.on_click(_Click())
        await pilot.pause()
        assert not body.has_class("hidden")
        assert "▾" in prefix(), "раскрыт → стрелка вниз"
        # повторный клик → свёрнут, стрелка снова вправо
        marker.on_click(_Click())
        await pilot.pause()
        assert body.has_class("hidden")
        assert "▸" in prefix(), "свёрнут → стрелка вправо"


@pytest.mark.asyncio
async def test_preview_enter_resumes_even_with_summary():
    """Enter в превью открывает тред (resume=True), даже если есть маркер сжатия:
    маркер не focusable, Enter не перехватывается toggle'ом."""
    pytest.importorskip("textual")
    from gc_tui.screens.preview import PreviewScreen
    from textual.app import App

    result = {}
    segments = [
        {
            "index": 0,
            "messages": [_rec({"role": "user", "content": "q", "id": "m0"})],
            "compacted_by": {
                "ts": "t",
                "messages_in_segment": 1,
                "synthesis": "СВОДКА",
            },
        },
        {
            "index": 1,
            "messages": [_rec({"role": "user", "content": "a", "id": "m1"})],
            "compacted_by": None,
        },
    ]

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=segments, thread_id="t", project_path="p"
                ),
                lambda v: result.update(v=v),
            )

    async with _A().run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] is True


@pytest.mark.asyncio
async def test_preview_enter_resumes_when_row_focused():
    """Enter резюмит тред даже когда в фокусе focusable-строка (тул/проза):
    приоритетный binding экрана срабатывает раньше on_key строки."""
    pytest.importorskip("textual")
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App

    result = {}
    segments = [
        {
            "index": 0,
            "compacted_by": None,
            "messages": [
                _rec(
                    {
                        "type": "function_call",
                        "id": "fc1",
                        "call_id": "c1",
                        "name": "read_file",
                        "arguments": '{"path": "x.py"}',
                    }
                ),
            ],
        }
    ]

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=segments, thread_id="t", project_path="p"
                ),
                lambda v: result.update(v=v),
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        row = scr.query_one(ToolCallRow)
        row.focus()
        await pilot.pause()
        assert scr.focused is row
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] is True


@pytest.mark.asyncio
async def test_preview_mounts_nested_subagent_rows():
    """Превью с sidecar-records показывает вложенные вызовы субагента
    и развёрнутый чек-лист (spec 2026-07-31 §4, канал превью)."""
    pytest.importorskip("textual")
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App

    segments = [
        {
            "index": 0,
            "compacted_by": None,
            "messages": [
                _rec(
                    {
                        "type": "function_call",
                        "id": "fc1",
                        "call_id": "parent1",
                        "name": "subagent_worker",
                        "arguments": '{"task": "x"}',
                    }
                ),
            ],
        }
    ]
    records = [
        {
            "parent_call_id": "parent1",
            "seq": 1,
            "kind": "call",
            "payload": {
                "call_id": "n1",
                "name": "fs_read_file",
                "arguments": {"path": "a.py"},
            },
        },
        {
            "parent_call_id": "parent1",
            "seq": 2,
            "kind": "call",
            "payload": {
                "call_id": "n2",
                "name": "session_todo",
                "arguments": {
                    "todos": [{"content": "step", "status": "pending"}]
                },
            },
        },
    ]

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=segments,
                    thread_id="t",
                    project_path="p",
                    subagent_records=records,
                )
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        nested_tools = scr.query(ToolCallRow).filter(".nested-tool")
        # fs_read_file — член вложенного блока `Explored` (P4 §7).
        assert [r.mode for r in nested_tools] == ["header", "member"]
        todo_rows = scr.query(".line-todo-nested")
        assert len(todo_rows) == 1  # развёрнутый чек-лист


@pytest.mark.asyncio
async def test_preview_without_records_unchanged():
    """Без records превью рендерится как раньше (ни одного nested-виджета)."""
    pytest.importorskip("textual")
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App

    segments = [
        {
            "index": 0,
            "compacted_by": None,
            "messages": [
                _rec(
                    {
                        "type": "function_call",
                        "id": "fc1",
                        "call_id": "parent1",
                        "name": "subagent_worker",
                        "arguments": '{"task": "x"}',
                    }
                ),
            ],
        }
    ]

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=segments, thread_id="t", project_path="p"
                )
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        assert len(scr.query(ToolCallRow).filter(".nested-tool")) == 0
        assert len(scr.query(".line-todo-nested")) == 0


@pytest.mark.asyncio
async def test_preview_escape_false():
    pytest.importorskip("textual")
    from gc_tui.screens.preview import PreviewScreen
    from textual.app import App

    result = {}

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(segments=[], thread_id="t", project_path="p"),
                lambda v: result.update(v=v),
            )

    async with _A().run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
    assert result["v"] is False
