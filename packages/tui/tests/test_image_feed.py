"""Лента: картинки реплики — подсветка плашек, подстроки, восстановление треда, упавшая
реплика, ответ на вопрос агента, превью, экспорт; сводки строки fs_view_image
(визуальный контракт картинок §3–§4, дизайн §12, §20.3)."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.color import Color
from textual.app import App
from textual.containers import VerticalScroll

from gc_core.attachments.model import ORIGIN_TOOL, TOOL_LABEL, chip_text
from gc_core.attachments.texts import (
    TOOL_NO_IMAGE_STORE,
    TOOL_NO_VISION,
    TOOL_TOO_MANY_IN_STEP,
    TOOL_VISION_FAILED,
)
from gc_core.vision.errors import (
    ERR_NO_IMAGE_STORE,
    ERR_NO_VISION_MODEL,
    ERR_TOO_MANY_IMAGES,
    ERR_VISION_FAILED,
    REASON_EMPTY_RESPONSE,
    REASON_HTTP,
)
from gc_tui.app import TuiApp
from gc_tui.palette import ACCENT, DIM
from gc_tui.services.export import export_thread
from gc_tui.services.trace_mapper import (
    ROW_NO_IMAGE_STORE,
    ROW_NO_VISION,
    ROW_TOO_MANY_IMAGES,
    ROW_VISION_FAILED,
    collect_image_index,
    collect_render_context,
    map_message,
)
from trace_readonly import render_thread_readonly
from gc_tui.widgets.user_reply import UserReplyContainer
from image_helpers import make_attachment_file
from gc_testkit.tui_support import tui_settings

TS = "2026-09-24T10:00:00+00:00"


def _ask_pair(response: str, *, call_id: str = "call_1") -> list[dict]:
    """Вопрос агента и ответ человека в форме chat_completions (имя тула — в output)."""
    return [
        {
            "id": "fc1",
            "type": "function_call",
            "name": "turn_ask_human",
            "call_id": call_id,
            "arguments": json.dumps({"question": "Покажите ошибку?"}),
        },
        {
            "id": "fo1",
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(
                {
                    "tool": "turn_ask_human",
                    "ok": True,
                    "error": None,
                    "question": "Покажите ошибку?",
                    "response": response,
                },
                ensure_ascii=False,
            ),
        },
    ]


def _technical_with(att) -> dict:
    """Техническое сообщение батча с картинкой (дизайн §7.1): в ленте скрыто."""
    return {
        "id": "t1",
        "role": "user",
        "content": '<technical type="technical">x</technical>',
        "attachments": [att.to_dict()],
    }


def _write_thread(
    threads: Path, tid: str, payloads: list[dict], *, tui: dict | None = None
) -> None:
    threads.mkdir(exist_ok=True)
    with open(threads / f"data_cli_{tid}.jsonl", "w", encoding="utf-8") as f:
        for i, payload in enumerate(payloads, start=1):
            f.write(
                json.dumps(
                    {
                        "type": "message",
                        "thread_id": tid,
                        "system": "cli",
                        "seq": i,
                        "ts": TS,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    (threads / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": TS,
                "messages_count": len(payloads),
                "tui": tui or {},
            }
        ),
        encoding="utf-8",
    )


def _app(threads: Path) -> TuiApp:
    return TuiApp(
        threads_dir=str(threads), system="cli", settings=tui_settings({})
    )


# ---- mapper ----


def test_user_message_event_carries_attachments(tmp_path):
    att = make_attachment_file(8, tmp_path)
    ev = map_message(
        {
            "role": "user",
            "content": f"{chip_text(8)} глянь",
            "attachments": [att.to_dict()],
        },
        no=1,
        ts=TS,
    )
    assert ev.kind == "user"
    assert ev.attachments == (att,)


def test_skill_message_event_carries_attachments(tmp_path):
    att = make_attachment_file(8, tmp_path)
    ev = map_message(
        {
            "role": "user",
            "content": "<инструкция скилла>\n" + chip_text(8),
            "display_content": chip_text(8),
            "attachments": [att.to_dict()],
        },
        no=1,
        ts=TS,
    )
    assert ev.attachments == (att,)


def test_ask_human_answer_links_images_by_chip_numbers(tmp_path):
    att = make_attachment_file(3, tmp_path)
    payloads = [*_ask_pair(f"{chip_text(3)} вот так"), _technical_with(att)]
    ev = map_message(
        payloads[1], no=2, ts=TS, context=collect_render_context(payloads)
    )
    assert ev.kind == "user"
    assert ev.attachments == (att,)


def test_ask_human_answer_without_chips_links_nothing(tmp_path):
    payloads = [
        *_ask_pair("да"),
        _technical_with(make_attachment_file(3, tmp_path)),
    ]
    ev = map_message(
        payloads[1], no=2, ts=TS, context=collect_render_context(payloads)
    )
    assert ev.attachments == ()


def test_image_index_skips_agent_snapshots(tmp_path):
    """Снимки агента (origin=tool) делят одну подпись — в индекс сцепки не идут."""
    snap = replace(
        make_attachment_file(5, tmp_path), origin=ORIGIN_TOOL, label=TOOL_LABEL
    )
    assert collect_image_index([_technical_with(snap)]) == {}


def _view_image_error(error: dict) -> str:
    ev = map_message(
        {
            "id": "fo",
            "type": "function_call_output",
            "call_id": "c",
            "output": json.dumps(
                {"tool": "fs_view_image", "ok": False, "error": error}
            ),
        },
        no=1,
        ts=TS,
    )
    assert ev.kind == "error"
    return ev.summary


def test_no_vision_model_row_is_in_russian():
    assert (
        _view_image_error(
            {"type": ERR_NO_VISION_MODEL, "message": TOOL_NO_VISION}
        )
        == ROW_NO_VISION
    )


def test_view_image_refusals_are_in_russian():
    assert _view_image_error(
        {
            "type": ERR_TOO_MANY_IMAGES,
            "message": TOOL_TOO_MANY_IN_STEP.format(n=3),
            "limit": 3,
        }
    ) == ROW_TOO_MANY_IMAGES.format(n=3)
    assert (
        _view_image_error(
            {"type": ERR_NO_IMAGE_STORE, "message": TOOL_NO_IMAGE_STORE}
        )
        == ROW_NO_IMAGE_STORE
    )


@pytest.mark.parametrize(
    "reason, shown",
    [
        ("ReadTimeout", "таймаут"),
        ("TimeoutError", "таймаут"),
        ("APITimeoutError", "таймаут"),
        (REASON_HTTP.format(status=503), "ошибка сервера 503"),
        (REASON_EMPTY_RESPONSE, "пустой ответ"),
        ("ConnectError", "нет связи"),
        ("APIConnectionError", "нет связи"),
        (REASON_HTTP.format(status=429), "HTTP 429"),
        ("ValueError", "ValueError"),
    ],
)
def test_vision_failure_reason_is_shown_in_russian(reason, shown):
    """Причина — из `error.reason`, не из английского текста для модели; частые —
    по-русски, прочие — как есть (визуальный контракт §4)."""
    error = {
        "type": ERR_VISION_FAILED,
        "message": TOOL_VISION_FAILED.format(reason=reason),
        "reason": reason,
    }
    assert _view_image_error(error) == ROW_VISION_FAILED.format(reason=shown)


def test_trace_mapper_does_not_load_the_view_image_tool():
    """Типы ошибок зрения — из лёгкого `gc_core.vision.errors`: лента не грузит тул
    просмотра (а с ним всё ядро) ради двух констант."""
    probe = (
        "import sys, gc_tui.services.trace_mapper; "
        "print('gc_core.tools.fs.view_image' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


# ---- рендер реплики ----


def _reply_with_two_images(tmp_path):
    a8 = make_attachment_file(8, tmp_path)
    a9 = make_attachment_file(
        9,
        tmp_path,
        source="clipboard",
        mime="image/jpeg",
        width=1024,
        height=768,
    )
    text = f"{chip_text(8)} {chip_text(9)} вот, что в файле, проверь сам"
    return UserReplyContainer(text, images=(a8, a9)), text, a8, a9


def test_reply_render_keeps_chips_in_the_text_colour_and_adds_sub_lines(
    tmp_path,
):
    """Плашки — цвета текста реплики (как ссылка до наведения), подстроки приглушены,
    источник тоже; каждый спан картинки несёт `image_id` для наведения и клика."""
    reply, text, a8, a9 = _reply_with_two_images(tmp_path)
    rendered = reply._band.render()
    assert rendered.plain == "\n".join(
        [
            f"› {text}",
            f"  └ {chip_text(8)} · скрин.png",
            f"  └ {chip_text(9)} · из буфера",
        ]
    )
    assert [
        rendered.plain[s.start : s.end]
        for s in rendered.spans
        if s.style.color == Color.parse(ACCENT)
    ] == ["› "]  # акцентом — только маркер реплики
    chips = [
        (
            rendered.plain[s.start : s.end],
            s.style.meta["image_id"],
            s.style.color,
        )
        for s in rendered.spans
        if rendered.plain[s.start : s.end].startswith("[Изображение")
    ]
    assert chips == [
        (chip_text(8), a8.id, None),
        (chip_text(9), a9.id, None),
        (chip_text(8), a8.id, None),
        (chip_text(9), a9.id, None),
    ]
    dim = [
        rendered.plain[s.start : s.end]
        for s in rendered.spans
        if s.style.color == Color.parse(DIM)
    ]
    assert dim == ["└ ", " · ", "скрин.png", "└ ", " · ", "из буфера"]


def test_hovering_an_image_highlights_all_its_places(tmp_path):
    """Общая подсветка: наведение на картинку красит акцентом все её места — плашку в
    тексте и её строку под репликой целиком; другая картинка не меняется."""
    reply, _text, a8, _a9 = _reply_with_two_images(tmp_path)
    reply._set_image_hover(a8.id)
    rendered = reply._band.render()
    accent = [
        rendered.plain[s.start : s.end]
        for s in rendered.spans
        if s.style.color == Color.parse(ACCENT)
    ]
    assert accent == [
        "› ",
        chip_text(8),
        "└ ",
        chip_text(8),
        " · ",
        "скрин.png",
    ]


def test_reply_without_images_renders_as_before():
    assert (
        UserReplyContainer("первая\nвторая")._band.render().plain
        == "› первая\n  вторая"
    )


# ---- восстановление, упавшая реплика, сверка pending ----


@pytest.mark.asyncio
async def test_restore_renders_reply_images(tmp_path):
    threads = tmp_path / "threads"
    att = make_attachment_file(8, tmp_path)
    _write_thread(
        threads,
        "a" * 32,
        [
            {
                "id": "u1",
                "role": "user",
                "content": f"{chip_text(8)} глянь",
                "attachments": [att.to_dict()],
            }
        ],
    )
    async with _app(threads).run_test() as pilot:
        pilot.app.screen.attach_active_task("a" * 32)
        await pilot.pause()
        assert [
            w._images for w in pilot.app.screen.query(UserReplyContainer)
        ] == [(att,)]


@pytest.mark.asyncio
async def test_restore_links_ask_human_answer_images(tmp_path):
    threads = tmp_path / "threads"
    att = make_attachment_file(3, tmp_path)
    _write_thread(
        threads,
        "b" * 32,
        [
            {"id": "u1", "role": "user", "content": "почини"},
            *_ask_pair(f"{chip_text(3)} вот так"),
            _technical_with(att),
        ],
    )
    async with _app(threads).run_test() as pilot:
        pilot.app.screen.attach_active_task("b" * 32)
        await pilot.pause()
        replies = list(pilot.app.screen.query(UserReplyContainer))
        assert [r._text for r in replies] == [
            "почини",
            f"{chip_text(3)} вот так",
        ]
        assert replies[-1]._images == (att,)


@pytest.mark.asyncio
async def test_failed_reply_remounts_with_images_from_the_pending_record(
    tmp_path,
):
    threads = tmp_path / "threads"
    att = make_attachment_file(8, tmp_path)
    text = f"{chip_text(8)} потерянное"
    _write_thread(
        threads,
        "c" * 32,
        [],
        tui={
            "pending_user_messages": [
                {
                    "client_id": "x",
                    "content": text,
                    "wire_content": text,
                    "ts_utc_iso": TS,
                    "status": "pending",
                    "attachments": [att.to_dict()],
                }
            ]
        },
    )
    async with _app(threads).run_test() as pilot:
        pilot.app.screen.attach_active_task("c" * 32)
        await pilot.pause()
        [reply] = list(pilot.app.screen.query(UserReplyContainer))
        assert reply.state == "failed"
        assert reply._images == (att,)


@pytest.mark.asyncio
async def test_pending_reply_takes_thread_copies_of_images_on_reconcile(
    tmp_path,
):
    threads = tmp_path / "threads"
    tid = "d" * 32
    _write_thread(threads, tid, [])
    staged = make_attachment_file(8, tmp_path)
    committed = replace(
        staged, path=str(threads / "images" / f"cli_{tid}_{staged.id}.png")
    )
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()
        text = f"{chip_text(8)} глянь"
        cid = scr._track_pending(
            display_text=text, wire_content=text, images=(staged,)
        )
        widget = scr._mount_user_reply(
            text, state="pending", client_id=cid, images=(staged,)
        )
        scr._render_record(
            {
                "ts": TS,
                "payload": {
                    "id": "u1",
                    "role": "user",
                    "content": text,
                    "attachments": [committed.to_dict()],
                },
            },
            live=True,
        )
        await pilot.pause()
        assert widget.state == "normal"
        assert widget._images == (committed,)
        assert len(scr.query(UserReplyContainer)) == 1


# ---- превью и экспорт ----


@pytest.mark.asyncio
async def test_readonly_preview_links_ask_human_answer_images(tmp_path):
    att = make_attachment_file(3, tmp_path)
    payloads = [
        {"id": "u1", "role": "user", "content": "почини"},
        *_ask_pair(f"{chip_text(3)} вот так"),
        _technical_with(att),
    ]
    records = [{"ts": TS, "payload": p} for p in payloads]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_thread_readonly(
                self.query_one("#t", VerticalScroll), records
            )

    async with _A().run_test() as pilot:
        replies = list(pilot.app.screen.query(UserReplyContainer))
        assert replies[-1]._images == (att,)


def test_txt_export_lists_image_paths(tmp_path):
    att = make_attachment_file(8, tmp_path)
    segments = [
        {
            "index": 0,
            "compacted_by": None,
            "messages": [
                {
                    "ts": TS,
                    "payload": {
                        "id": "u1",
                        "role": "user",
                        "content": f"{chip_text(8)} глянь",
                        "attachments": [att.to_dict()],
                    },
                }
            ],
        }
    ]
    out = export_thread(
        segments, fmt="txt", meta={}, thread_id="t", system="cli"
    )
    assert (
        f"    └ {chip_text(8)} · png 1456×819 · {att.path}" in out.splitlines()
    )


@pytest.mark.asyncio
async def test_preview_nested_reply_to_a_subagent_shows_its_images(tmp_path):
    """Реплика субагенту с картинкой в превью сессии — со строкой картинки."""
    from textual.containers import VerticalScroll

    from gc_tui.services.trace_render import merge_nested_readonly
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.user_reply import NestedUserReply

    att = make_attachment_file(4, tmp_path)
    records = [
        {
            "seq": 1,
            "parent_call_id": "call_sub",
            "kind": "user",
            "payload": {
                "role": "user",
                "content": f"{chip_text(4)} глянь",
                "attachments": [att.to_dict()],
            },
        }
    ]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            t = self.query_one("#t", VerticalScroll)
            parent = ToolCallRow(
                call_id="call_sub",
                label="subagent_worker",
                target="task",
            )
            t.mount(parent)
            merge_nested_readonly(t, records, {"call_sub": parent})

    async with _A().run_test() as pilot:
        await pilot.pause()
        [row] = pilot.app.screen.query(NestedUserReply)
        assert row._images == (att,)
