"""Дедуп preserved-реплики при рендере сегментов.

При сжатии последнее user-сообщение намеренно переносится в новый компакт-файл
(build_preserved_batch) — поэтому оно лежит и в архивном сегменте, и в live (тот
же id). Рендер сегментов рисует соседние сегменты сырьём → реплика дублировалась
вокруг маркера «контекст сжат». Дедуп по id убирает повтор (номер события у копий
разный — это render-счётчик, не identity)."""
import pytest


_DUP = "m_dup"


def _seg_archived():
    return {
        "index": 0,
        "messages": [{"payload": {"role": "user", "content": "A", "id": _DUP}}],
        "compacted_by": {"ts": "", "messages_in_segment": 72, "synthesis": "СИНТЕЗ"},
    }


def _seg_live():
    return {
        "index": 1,
        "messages": [
            {"payload": {"role": "user", "content": "A", "id": _DUP}},  # preserved-дубль
            {"payload": {"id": "a1", "type": "message", "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": "ответ",
                                      "annotations": []}]}},
        ],
        "compacted_by": None,
    }


@pytest.mark.asyncio
async def test_main_render_segments_dedupes_preserved_reply(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.user_reply import UserReplyContainer

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_segments([_seg_archived(), _seg_live()])
        await pilot.pause()
        # реплика "A" (один id) отрисована один раз, не две
        assert len(scr.query(UserReplyContainer)) == 1
        # маркер сжатия на месте
        assert any(e.kind == "summary" for e in scr._task_events)


@pytest.mark.asyncio
async def test_readonly_render_segments_dedupes_preserved_reply():
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll
    from tui.services.trace_render import render_segments_readonly
    from tui.widgets.user_reply import UserReplyContainer

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(
                self.query_one("#t", VerticalScroll), [_seg_archived(), _seg_live()])

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        assert len(scr.query(UserReplyContainer)) == 1
