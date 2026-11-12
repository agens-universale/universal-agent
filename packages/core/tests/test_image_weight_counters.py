"""Картинка занимает контекст, хотя в content её нет (спека §5): ваша картинка
весит CONTEXT_CHARS_PER_IMAGE в счётчиках объёма — пороге суммаризации и
live-агрегатах треда; снимок агента — CONTEXT_CHARS_PER_NOTE: пикселями он живёт
только текущий ход, дальше модели уходит записка (ревью 2026-10-01)."""

import os
import tempfile
from types import SimpleNamespace

from gc_core.attachments.model import (
    CONTEXT_CHARS_PER_IMAGE,
    CONTEXT_CHARS_PER_NOTE,
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
)
from gc_core.message_format.serialization import (
    calc_history_chars_for_trigger,
    message_raw_payload_chars_for_trigger,
)
from gc_core.persistence.live_volume import live_volume_from_messages
from gc_core.summarize.compaction import history_exceeds_summary_trigger
from gc_core.threads.models import RuntimeStatus
from gc_core.threads.runtime_rules import bumped_live_volume


def _att(n: int, origin: str = ORIGIN_USER) -> dict:
    att_id = f"{n:016x}"
    return Attachment(
        id=att_id,
        kind="image",
        mime="image/png",
        width=10,
        height=10,
        size=1000,
        path=os.path.join(
            tempfile.gettempdir(), "gc-images", f"cli_t_{att_id}.png"
        ),
        origin=origin,
        label=user_label(n) if origin == ORIGIN_USER else TOOL_LABEL,
        source="clipboard",
        orig_width=10,
        orig_height=10,
    ).to_dict()


PLAIN = {
    "role": "user",
    "id": "m1",
    "content": "[Изображение #1] [Изображение #2]",
}
WITH_IMAGES = {**PLAIN, "attachments": [_att(1), _att(2)]}


def test_each_attachment_weighs_context_chars_per_image():
    assert message_raw_payload_chars_for_trigger(WITH_IMAGES) == (
        message_raw_payload_chars_for_trigger(PLAIN)
        + 2 * CONTEXT_CHARS_PER_IMAGE
    )


def test_a_tool_snapshot_weighs_a_note():
    technical = {
        "role": "user",
        "id": "t1",
        "content": '<technical type="technical">shot</technical>',
    }
    assert message_raw_payload_chars_for_trigger(
        {**technical, "attachments": [_att(7, ORIGIN_TOOL)]}
    ) == (
        message_raw_payload_chars_for_trigger(technical)
        + CONTEXT_CHARS_PER_NOTE
    )


def test_live_volume_aggregates_count_the_same_weight():
    messages = [
        WITH_IMAGES,
        {
            "type": "message",
            "role": "assistant",
            "id": "a1",
            "content": "вижу",
        },
    ]
    expected = (
        len(PLAIN["content"]) + 2 * CONTEXT_CHARS_PER_IMAGE + len("вижу")
    )
    assert calc_history_chars_for_trigger(messages) == expected
    assert (
        live_volume_from_messages(messages)["live_history_chars"] == expected
    )
    runtime = bumped_live_volume(RuntimeStatus(), messages)
    assert runtime.live_history_chars == expected


def test_image_weight_reaches_the_summary_trigger():
    state = SimpleNamespace(messages=[{**PLAIN, "attachments": [_att(1)]}])
    assert (
        history_exceeds_summary_trigger(
            state=state, summary_trigger_chars=CONTEXT_CHARS_PER_IMAGE
        )
        is True
    )
