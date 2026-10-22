"""Превью сессии (Ctrl+P): скилл — именем в реплике (спека P3 §2.3).

Паритет с MainScreen._render_record: `skill_loaded` записи едет в событии
(`TraceEvent.skill`) и рисуется первым словом реплики; отдельной строки
`[skill]` нет. Каталога скиллов у превью нет — имя полное.
"""

import pytest


def _rec(payload, ts="2026-06-01T12:00:00Z"):
    """Envelope-запись вокруг payload."""
    return {
        "type": "message",
        "thread_id": "t",
        "system": "cli",
        "seq": 1,
        "ts": ts,
        "payload": payload,
    }


def _first_line(reply) -> str:
    line = reply._reply_lines(80)[0]
    return (line.prefix + line.body).plain


async def _replies(records):
    from textual.app import App
    from textual.containers import VerticalScroll
    from trace_readonly import render_thread_readonly

    from gc_tui.widgets.trace_row import TraceRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_thread_readonly(
                self.query_one("#t", VerticalScroll), records
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        rows = [str(w._content) for w in scr.query(TraceRow)]
        return list(scr.query(UserReplyContainer)), rows


@pytest.mark.asyncio
async def test_skill_plus_prompt_is_one_reply_with_skill_name():
    pytest.importorskip("textual")
    replies, rows = await _replies(
        [
            _rec(
                {
                    "role": "user",
                    "content": "<skill>\nдавай",
                    "display_content": "давай",
                    "skill_loaded": "pack:qa-x",
                    "id": "m1",
                }
            )
        ]
    )
    assert len(replies) == 1
    assert _first_line(replies[0]) == "› /pack:qa-x давай"
    assert replies[0]._reply_lines(80)[0].body.spans == []
    assert not any("[skill]" in r or "загружен" in r for r in rows)


@pytest.mark.asyncio
async def test_mid_run_delivery_is_reply_of_skill_name_only():
    pytest.importorskip("textual")
    replies, rows = await _replies(
        [
            _rec(
                {
                    "role": "user",
                    "content": "<skill>",
                    "display_content": "",
                    "skill_loaded": "qa-x",
                    "id": "m2",
                }
            )
        ]
    )
    assert [_first_line(r) for r in replies] == ["› /qa-x"]
    assert not any("загружен" in r for r in rows)


@pytest.mark.asyncio
async def test_normal_user_record_has_no_skill_name():
    pytest.importorskip("textual")
    replies, _ = await _replies(
        [_rec({"role": "user", "content": "привет", "id": "m3"})]
    )
    assert [_first_line(r) for r in replies] == ["› привет"]
