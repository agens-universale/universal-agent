"""Картинки в ленте: реплика с двумя картинками и серая строка «изображение не
просмотрено»; ответ на вопрос агента с картинкой (визуальный контракт картинок §3–§4)."""

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_testkit.tui_fixtures.stub_executor import StubExecutor
from gc_testkit.tui_support import tui_settings


def _app(tmp_path, monkeypatch, *, scenario: str, tid: str, message: str):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    # cwd-чип в футере фиксируем — иначе кадр машинно-зависим.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    fake = StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario=scenario
    )
    fake.run([message], thread_id=tid)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )


def test_image_reply_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_core.vision.notes import NOTE_MAIN_BLIND, TraceNote

    tid = "img_snap"
    app = _app(
        tmp_path,
        monkeypatch,
        scenario="image_reply",
        tid=tid,
        message="вот, что в файле, проверь сам",
    )

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task(tid)
        # Мигание курсора поля — таймер 0.5 с, фаза в кадре от нагрузки;
        # без мигания курсор виден всегда (приём `test_snapshot_forms`).
        pilot.app.screen.query_one("#cmdinput").cursor_blink = False
        pilot.app._trace_note_sink.push(
            tid, TraceNote(NOTE_MAIN_BLIND.format(alias="glm"), "reply")
        )
        await pilot.pause(0.6)  # тик опроса (250 мс) снимает очередь пометок

    assert snap_compare(app, terminal_size=(120, 32), run_before=setup)


def test_image_reply_narrow_snapshot(snap_compare, tmp_path, monkeypatch):
    """Узкое окно: реплика переносится своей раскладкой — плашки целиком,
    продолжение и строки картинок под текстом, а не под `> `."""
    tid = "img_narrow_snap"
    app = _app(
        tmp_path,
        monkeypatch,
        scenario="image_reply",
        tid=tid,
        message="вот, что в файле, проверь сам",
    )

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task(tid)
        # Мигание курсора поля — таймер 0.5 с, фаза в кадре от нагрузки;
        # без мигания курсор виден всегда (приём `test_snapshot_forms`).
        pilot.app.screen.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.5)

    assert snap_compare(app, terminal_size=(50, 32), run_before=setup)


def test_ask_human_image_answer_snapshot(snap_compare, tmp_path, monkeypatch):
    tid = "ah_img_snap"
    app = _app(
        tmp_path,
        monkeypatch,
        scenario="ask_human_image_answer",
        tid=tid,
        message="почини вход",
    )

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task(tid)
        # Мигание курсора поля — таймер 0.5 с, фаза в кадре от нагрузки;
        # без мигания курсор виден всегда (приём `test_snapshot_forms`).
        pilot.app.screen.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.5)

    assert snap_compare(app, terminal_size=(120, 32), run_before=setup)


@pytest.mark.parametrize(
    "tone, plain, style",
    [
        ("info", "deepseek не принимает изображения", "DIM"),
        ("warn", "! deepseek не принимает изображения", "WARN"),
    ],
)
async def test_trace_note_tone_paints_the_reply_consequence(
    tmp_path, monkeypatch, tone, plain, style
):
    """Пометка под репликой с картинкой: `info` — серое следствие без `!`,
    `warn` — прежнее оранжевое `! …`."""
    from gc_core.vision.notes import TraceNote
    from gc_tui import palette
    from gc_tui.widgets.user_reply import UserReplyContainer

    tid = "img_tone"
    app = _app(
        tmp_path,
        monkeypatch,
        scenario="image_reply",
        tid=tid,
        message="вот, что в файле, проверь сам",
    )
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        pilot.app.screen.attach_active_task(tid)
        pilot.app._trace_note_sink.push(
            tid,
            TraceNote(
                "deepseek не принимает изображения", "reply", None, tone
            ),
        )
        await pilot.pause(0.6)
        reply = [
            r
            for r in pilot.app.screen.query(UserReplyContainer)
            if r.has_images
        ][-1]
        body = reply._consequences[-1]

    assert body.plain == plain
    assert {str(s.style) for s in body.spans} == {getattr(palette, style)}
