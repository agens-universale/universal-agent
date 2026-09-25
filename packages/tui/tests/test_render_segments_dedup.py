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
        "messages": [
            {"payload": {"role": "user", "content": "A", "id": _DUP}}
        ],
        "compacted_by": {
            "ts": "",
            "messages_in_segment": 72,
            "synthesis": "СИНТЕЗ",
        },
    }


def _seg_live():
    return {
        "index": 1,
        "messages": [
            {
                "payload": {"role": "user", "content": "A", "id": _DUP}
            },  # preserved-дубль
            {
                "payload": {
                    "id": "a1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "ответ",
                            "annotations": [],
                        }
                    ],
                }
            },
        ],
        "compacted_by": None,
    }


@pytest.mark.asyncio
async def test_main_render_segments_dedupes_preserved_reply(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_segments([_seg_archived(), _seg_live()])
        await pilot.pause()
        # реплика "A" (один id) отрисована один раз, не две
        assert len(scr.query(UserReplyContainer)) == 1
        # маркер сжатия на месте
        assert any(e.kind == "summary" for e in scr._task_events)


def _seg_user(idx, content, mid, n_compacted):
    """Сегмент с одной уникальной user-репликой; n_compacted=None → live (без маркера)."""
    return {
        "index": idx,
        "messages": [
            {"payload": {"role": "user", "content": content, "id": mid}}
        ],
        "compacted_by": (
            {
                "ts": "",
                "messages_in_segment": n_compacted,
                "synthesis": f"S{idx}",
            }
            if n_compacted
            else None
        ),
    }


@pytest.mark.asyncio
async def test_render_segments_renders_all_not_just_last_two(tmp_path):
    """B-фикс: после 2+ компакций самый старый сегмент НЕ схлопывается в маркер —
    весь трейс рендерится сырьём (а не только последние 2 сегмента)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # 3 сегмента (= 2 компакции): OLD до #1, MID между #1 и #2, LIVE после #2.
        scr._render_segments(
            [
                _seg_user(0, "OLD", "m0", 47),
                _seg_user(1, "MID", "m1", 22),
                _seg_user(2, "LIVE", "m2", None),
            ]
        )
        await pilot.pause()
        # ВСЕ три реплики отрисованы (старый сегмент не потерян)
        assert len(scr.query(UserReplyContainer)) == 3
        # маркеры компакции тоже на месте (2 штуки)
        assert sum(1 for e in scr._task_events if e.kind == "summary") == 2


@pytest.mark.asyncio
async def test_readonly_render_segments_dedupes_preserved_reply():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import render_segments_readonly
    from gc_tui.widgets.user_reply import UserReplyContainer
    from textual.app import App
    from textual.containers import VerticalScroll

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(
                self.query_one("#t", VerticalScroll),
                [_seg_archived(), _seg_live()],
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        assert len(scr.query(UserReplyContainer)) == 1
