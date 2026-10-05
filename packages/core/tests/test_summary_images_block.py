"""Блок <images> в теле сводки (спека §6): ваши картинки из сворачиваемой части —
по строке-записке «вне контекста», детерминированно, без LLM."""

import os
import tempfile

from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
    wh_text,
)
from gc_core.attachments.texts import (
    STUB_OUT_OF_CONTEXT,
    SUMMARY_IMAGES_CLOSE,
    SUMMARY_IMAGES_OPEN,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.states import State
from gc_core.summarize.compaction import apply_summary_state_patch
from gc_core.summarize.summary_message_body import (
    build_dialog_summary_content,
    render_summary_images_block,
)


def _attachment(n: int, *, origin: str = ORIGIN_USER) -> Attachment:
    att_id = f"{n:016x}"
    return Attachment(
        id=att_id,
        kind="image",
        mime="image/png",
        width=1456,
        height=819,
        size=214_880,
        path=os.path.join(
            tempfile.gettempdir(), "gc-images", f"cli_t_{att_id}.png"
        ),
        origin=origin,
        label=user_label(n) if origin == ORIGIN_USER else TOOL_LABEL,
        source="clipboard",
        orig_width=1456,
        orig_height=819,
    )


def _line(att: Attachment) -> str:
    return STUB_OUT_OF_CONTEXT.format(
        label=att.label, wh=wh_text(att), path=att.path
    )


def _with(mid: str, text: str, *atts: Attachment) -> dict:
    return {
        "role": "user",
        "id": mid,
        "content": text,
        "attachments": [a.to_dict() for a in atts],
    }


def test_block_lists_only_user_images():
    user_att, tool_att = _attachment(1), _attachment(7, origin=ORIGIN_TOOL)
    messages = [
        _with("m1", "[Изображение #1]", user_att),
        _with("t1", '<technical type="technical">shot</technical>', tool_att),
    ]
    assert render_summary_images_block(messages) == "\n".join(
        [SUMMARY_IMAGES_OPEN, _line(user_att), SUMMARY_IMAGES_CLOSE]
    )


def test_no_user_images_no_block():
    assert (
        render_summary_images_block(
            [{"role": "user", "id": "m1", "content": "привет"}]
        )
        == ""
    )


def test_lines_of_the_previous_summary_are_carried_over():
    """Прежняя сводка тоже сворачивается: её строки <images> переезжают дословно,
    иначе после второй компакции указатель на раннюю картинку жил бы только в
    пересказе суммаризатора."""
    old, new = _attachment(1), _attachment(2)
    previous_summary = {
        "role": "user",
        "id": "m_sum",
        "content": wrap_technical_user_content(
            "Summarized context from previous dialogue.\n"
            + "\n".join(
                [SUMMARY_IMAGES_OPEN, _line(old), SUMMARY_IMAGES_CLOSE]
            ),
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
    }
    messages = [
        previous_summary,
        _with("m2", "[Изображение #2]", new),
        _with("m3", "снова [Изображение #1]", old),
    ]
    assert render_summary_images_block(messages) == "\n".join(
        [SUMMARY_IMAGES_OPEN, _line(old), _line(new), SUMMARY_IMAGES_CLOSE]
    )


def test_previous_summary_line_of_an_image_that_comes_back_is_dropped():
    """Картинку X свернули раньше, а сейчас её снова вставили в сохраняемый батч:
    строка прежней сводки «X вне контекста» противоречила бы живой X (ревью
    2026-10-01). Id картинки — в имени её файла, по нему и сверяем."""
    old = _attachment(1)
    previous_summary = {
        "role": "user",
        "id": "m_sum",
        "content": wrap_technical_user_content(
            "Summarized context from previous dialogue.\n"
            + "\n".join(
                [SUMMARY_IMAGES_OPEN, _line(old), SUMMARY_IMAGES_CLOSE]
            ),
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
    }
    kept = _with("m5", "снова [Изображение #1]", old)
    assert (
        render_summary_images_block([previous_summary, kept], kept=[kept])
        == ""
    )


def test_images_block_typed_by_the_user_is_not_carried_over():
    typed = {
        "role": "user",
        "id": "m1",
        "content": "\n".join(
            [SUMMARY_IMAGES_OPEN, "fake", SUMMARY_IMAGES_CLOSE]
        ),
    }
    assert render_summary_images_block([typed]) == ""


def test_block_goes_before_last_dialog_steps():
    block = "\n".join([SUMMARY_IMAGES_OPEN, "line", SUMMARY_IMAGES_CLOSE])
    body = build_dialog_summary_content(
        summary_text="S", last_dialog_steps="tail", images_block=block
    )
    assert block in body
    assert body.index(SUMMARY_IMAGES_OPEN) < body.index("<last_dialog_steps>")


def test_compaction_summary_lists_user_images_of_the_collapsed_history():
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    user_att, tool_att = _attachment(1), _attachment(7, origin=ORIGIN_TOOL)
    state.messages = [
        _with("m1", "[Изображение #1] что тут?", user_att),
        _with("t1", '<technical type="technical">shot</technical>', tool_att),
        {
            "type": "message",
            "role": "assistant",
            "id": "a1",
            "content": "кнопка съехала",
        },
    ]

    content = apply_summary_state_patch(
        state=state,
        raw_summary="S",
        last_dialog_steps="",
        summarized_messages_count=3,
    )

    assert (
        "\n".join([SUMMARY_IMAGES_OPEN, _line(user_att), SUMMARY_IMAGES_CLOSE])
        in content
    )
    assert _line(tool_att) not in content
    assert "attachments" not in state.messages[-1]


def test_block_skips_images_of_messages_that_come_back_whole():
    """Сжатие перед запросом возвращает текущий батч как есть: его картинки остаются
    живыми — записка «вне контекста» в сводке им противоречила бы (ревью 2026-10-01)."""
    old, fresh = _attachment(1), _attachment(2)
    kept = _with("m2", "[Изображение #2] а тут?", fresh)
    messages = [_with("m1", "[Изображение #1]", old), kept]
    assert render_summary_images_block(messages, kept=[kept]) == "\n".join(
        [SUMMARY_IMAGES_OPEN, _line(old), SUMMARY_IMAGES_CLOSE]
    )


def test_compaction_leaves_images_of_the_kept_batch_out_of_the_block():
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    old, fresh = _attachment(1), _attachment(2)
    kept = _with("m2", "[Изображение #2] а тут?", fresh)
    state.messages = [_with("m1", "[Изображение #1]", old), kept]

    content = apply_summary_state_patch(
        state=state,
        raw_summary="S",
        last_dialog_steps="",
        summarized_messages_count=2,
        kept_messages=[kept],
    )

    assert _line(old) in content
    assert _line(fresh) not in content
