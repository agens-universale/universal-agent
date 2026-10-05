"""Dirty state → _request_backend_canonical → outbound как в ``state.messages``.

Основной трейс шлёт канон as-is (включая ``<think>`` / ``reasoning_content``).
Проекцию без think делают derived-sink'и (саммари, digest, intent), не outbound.

Оговорка (спека §5): единственная проекция на выходе — вложения. Сообщение с
ключом ``attachments`` уходит картинками и/или записками
(``message_policy.attachments_outbound``) и без самого ключа; остальной канон — as-is.
"""

from __future__ import annotations

import copy
import os
import tempfile

from gc_core import processing_responses as pr
from gc_core.attachments.model import (
    ORIGIN_USER,
    Attachment,
    user_label,
    wh_text,
)
from gc_core.attachments.texts import STUB_NOBODY
from gc_core.message_format.serialization import (
    calc_history_chars_for_trigger,
    message_raw_payload_chars_for_trigger,
)
from gc_core.message_projection import project_assistant_text
from gc_testkit import run_coro

from .conftest import (
    DIRTY_THINK,
    USER_LITERAL_THINK,
    dirty_canonical_history,
    make_ctx_state,
)


def _assistant_contents(outbound_messages: list[dict]) -> list[str]:
    return [
        m["content"]
        for m in outbound_messages
        if isinstance(m, dict)
        and m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
    ]


def test_outbound_keeps_think_as_in_messages(capture_outbound):
    ctx, state = make_ctx_state()
    before = copy.deepcopy(state.messages)

    run_coro(
        pr._arequest_backend_canonical(
            [
                {
                    "id": "u2",
                    "type": "message",
                    "role": "user",
                    "content": "продолжай",
                }
            ],
            state,
            ctx,
        )
    )

    outbound = capture_outbound["messages"]
    assert outbound is not None
    assert capture_outbound["request_label"] == "main_agent"

    assistant_texts = _assistant_contents(outbound)
    assert assistant_texts, "ожидали assistant в outbound"
    joined = "\n".join(assistant_texts)
    assert DIRTY_THINK in joined
    assert "Сделаю tool call." in joined
    assert "Готово." in joined

    user_texts = [
        m["content"]
        for m in outbound
        if isinstance(m, dict)
        and m.get("role") == "user"
        and isinstance(m.get("content"), str)
    ]
    assert USER_LITERAL_THINK in user_texts

    assert state.messages == before
    assert any(
        DIRTY_THINK in str(m.get("content"))
        for m in state.messages
        if isinstance(m, dict)
    )


def test_trigger_volume_on_same_dirty_history_excludes_think():
    """Тот же dirty history: trigger-volume без think-span (согласован с for_model)."""
    history = dirty_canonical_history()
    chars = calc_history_chars_for_trigger(history)
    expected = sum(message_raw_payload_chars_for_trigger(m) for m in history)
    assert chars == expected

    assistant_projected = [
        project_assistant_text(m.get("content"))
        for m in history
        if m.get("role") == "assistant" and m.get("type") == "message"
    ]
    assert assistant_projected == ["Сделаю tool call.", "Готово."]
    assert all("<think" not in t for t in assistant_projected)

    dirty_assistant_lens = 0
    for m in history:
        if m.get("role") == "assistant" and m.get("type") == "message":
            raw = m.get("content")
            if isinstance(raw, list):
                dirty_assistant_lens += sum(
                    len(p.get("text", "")) for p in raw if isinstance(p, dict)
                )
            elif isinstance(raw, str):
                dirty_assistant_lens += len(raw)
    assert sum(len(t) for t in assistant_projected) < dirty_assistant_lens
    # think-span'ы не входят в trigger budget assistant'ов
    assert chars == (
        len(USER_LITERAL_THINK)
        + len("Сделаю tool call.")
        + len("read_file")
        + len('{"path":"README.md"}')
        + len("ok")
        + len("Готово.")
    )


def test_only_think_assistant_kept_in_outbound(capture_outbound):
    history = [
        {"id": "u1", "type": "message", "role": "user", "content": "hi"},
        {
            "id": "a1",
            "type": "message",
            "role": "assistant",
            "content": "<think>only reasoning</think>",
        },
    ]
    ctx, state = make_ctx_state(history=history)

    run_coro(pr._arequest_backend_canonical([], state, ctx))

    assistant_texts = _assistant_contents(capture_outbound["messages"])
    assert assistant_texts == ["<think>only reasoning</think>"]
    assert "<think>only reasoning</think>" in str(state.messages[1]["content"])


def test_attachments_are_the_only_outbound_projection(capture_outbound):
    """Оговорка инварианта: сообщение с ``attachments`` уходит запиской (у двойника
    ctx нет роутера — видеть некому), остальной канон — as-is, state не меняется."""
    att = Attachment(
        id="3f9a1c27b0e4d5a6",
        kind="image",
        mime="image/png",
        width=1456,
        height=819,
        size=214880,
        path=os.path.join(
            tempfile.gettempdir(), "gc-images", "cli_t_3f9a1c27b0e4d5a6.png"
        ),
        origin=ORIGIN_USER,
        label=user_label(8),
        source="clipboard",
        orig_width=1456,
        orig_height=819,
    )
    history = dirty_canonical_history() + [
        {
            "id": "u9",
            "role": "user",
            "content": "[Изображение #8] почему кнопка съехала?",
            "attachments": [att.to_dict()],
        },
    ]
    ctx, state = make_ctx_state(history=history)
    before = copy.deepcopy(state.messages)

    run_coro(pr._arequest_backend_canonical([], state, ctx))

    outbound = capture_outbound["messages"]
    assert DIRTY_THINK in "\n".join(_assistant_contents(outbound))
    assert outbound[-1] == {
        "role": "user",
        "content": STUB_NOBODY.format(
            label=att.label, wh=wh_text(att), path=att.path
        )
        + "\n[Изображение #8] почему кнопка съехала?",
    }
    assert state.messages == before
