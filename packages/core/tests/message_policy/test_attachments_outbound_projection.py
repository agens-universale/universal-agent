"""Проекция вложений на провод (спека §5): картинка — частью ``image_url`` или
текстовой запиской на своём месте; форма сообщения — по режиму API; ключ
``attachments`` на провод не уходит; удалённый файл — записка, а не падение хода."""

from gc_testkit import run_coro
import base64
import copy
import os
import tempfile

import pytest

from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
    wh_text,
)
from gc_core.attachments.normalize import NormalizedImage
from gc_core.attachments.store import ImageStore
from gc_core.attachments.texts import (
    STUB_ATTACHED_INLINE,
    STUB_FILE_MISSING,
    STUB_MODEL_BLIND,
    STUB_NOBODY,
    STUB_OUT_OF_CONTEXT,
    STUB_SHOWN_ELSEWHERE,
    TECH_LINE_TOOL,
)
from gc_core.message_policy.attachments_outbound import (
    project_outbound,
    request_carries_images,
)
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.model_router import ResolvedModel
from gc_core.utils.format_messages import (
    format_message_chat_completions_to_send_backend,
)
from gc_core.vision.audience import Audience

CHAT = "chat_completions"
PNG = b"\x89PNG projection test bytes"
_DIR = os.path.join(tempfile.gettempdir(), "gc-images")
MODEL = ResolvedModel(
    alias="vis",
    model="vendor/vis",
    url="http://llm.test/v1/chat/completions",
    authorization=None,
    backend_overrides={},
    tariff=None,
    source="test",
)
#: Модель запроса видит, тул просмотра в наборе.
SEES = Audience(native=MODEL, eyes=None, tool_visible=True)
#: Модель запроса видит, тула в наборе нет.
SEES_WITHOUT_TOOL = Audience(native=MODEL, eyes=None, tool_visible=False)
#: Модель запроса слепа, смотрят «глаза».
BLIND_WITH_EYES = Audience(native=None, eyes=MODEL, tool_visible=True)
#: Смотреть некому.
NOBODY = Audience(native=None, eyes=None, tool_visible=False)


def _read(_attachment: Attachment) -> bytes:
    return PNG


def _unreadable(_attachment: Attachment) -> None:
    return None


def _attachment(n: int, *, origin: str = ORIGIN_USER) -> Attachment:
    att_id = f"{n:016x}"
    return Attachment(
        id=att_id,
        kind="image",
        mime="image/png",
        width=1456,
        height=819,
        size=214_880,
        path=os.path.join(_DIR, f"cli_t_{att_id}.png"),
        origin=origin,
        label=user_label(n) if origin == ORIGIN_USER else TOOL_LABEL,
        source="clipboard" if origin == ORIGIN_USER else "shot.png",
        orig_width=2912,
        orig_height=1638,
    )


def _message(mid: str, text: str, *attachments: Attachment) -> dict:
    return {
        "role": "user",
        "id": mid,
        "content": text,
        "attachments": [a.to_dict() for a in attachments],
    }


def _note(template: str, att: Attachment) -> str:
    return template.format(label=att.label, wh=wh_text(att), path=att.path)


def _image_part(data: bytes = PNG) -> dict:
    return {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,"
            + base64.b64encode(data).decode("ascii")
        },
    }


def _pixels(att: Attachment, data: bytes = PNG) -> list[dict]:
    """Подпись картинки и её пиксели — две части подряд."""
    label = {"type": "text", "text": _note(STUB_ATTACHED_INLINE, att)}
    return [label, _image_part(data)]


def test_message_without_attachments_is_the_same_object():
    message = {"role": "user", "id": "m1", "content": "привет"}
    [projected] = project_outbound(
        [message],
        audience=SEES,
        live_ids=frozenset(),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected is message


def test_live_image_goes_as_image_part_before_the_text():
    att = _attachment(1)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1] почему кнопка съехала?", att)],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected == {
        "role": "user",
        "id": "m1",
        "content": [
            *_pixels(att),
            {
                "type": "text",
                "text": "[Изображение #1] почему кнопка съехала?",
            },
        ],
    }


def test_pixels_go_right_after_a_label_with_number_and_size():
    """Пиксели без подписи модель не связывает с номером в тексте: перед частью
    ``image_url`` — подпись в форме записок (решение владельца 2026-10-24)."""
    from dataclasses import replace

    att = replace(_attachment(8), width=1200, height=800)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #8] что тут?", att)],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected["content"][:2] == [
        {
            "type": "text",
            "text": "[Изображение #8 · png 1200×800 · attached inline]",
        },
        _image_part(),
    ]


def test_same_image_goes_as_pixels_once_in_its_latest_place():
    """Один файл в двух сообщениях (повторный просмотр старой картинки тулом, та же
    вставка снова) уходит пикселями один раз — в последнем вхождении; раньше —
    записка (ревью 2026-10-01: иначе старое сообщение возвращало пиксели в обход
    окна живости и картинка шла дважды)."""
    att = _attachment(1)
    first, again = project_outbound(
        [
            _message("m1", "[Изображение #1] что тут?", att),
            _message("m2", "[Изображение #1] и снова", att),
        ],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert first["content"] == "\n".join(
        [_note(STUB_SHOWN_ELSEWHERE, att), "[Изображение #1] что тут?"]
    )
    assert again["content"][:2] == _pixels(att)


def test_unreadable_latest_place_falls_back_to_an_earlier_readable_one():
    """Последнее вхождение не читается (мёртвый путь кармана) — пиксели уходят из
    раньшего читаемого, модель картинку видит (ревью 2026-10-01)."""
    from dataclasses import replace

    early = _attachment(1)
    late = replace(early, path=os.path.join(_DIR, "gone.png"))
    first, again = project_outbound(
        [
            _message("m1", "[Изображение #1] что тут?", early),
            _message("m2", "[Изображение #1] и снова", late),
        ],
        audience=SEES,
        live_ids=frozenset({early.id}),
        api_mode=CHAT,
        read_bytes=lambda att: None if att.path == late.path else PNG,
    )
    assert first["content"][:2] == _pixels(early)
    assert again["content"] == "\n".join(
        [_note(STUB_SHOWN_ELSEWHERE, late), "[Изображение #1] и снова"]
    )


def test_dead_attachment_of_a_mixed_message_is_a_text_note_in_its_place():
    old, fresh = _attachment(1), _attachment(2)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1] [Изображение #2]", old, fresh)],
        audience=SEES,
        live_ids=frozenset({fresh.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected["content"] == [
        {"type": "text", "text": _note(STUB_OUT_OF_CONTEXT, old)},
        *_pixels(fresh),
        {"type": "text", "text": "[Изображение #1] [Изображение #2]"},
    ]


def test_message_without_live_images_goes_as_one_string():
    first, second = _attachment(1), _attachment(2)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1] [Изображение #2]", first, second)],
        audience=SEES,
        live_ids=frozenset(),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected == {
        "role": "user",
        "id": "m1",
        "content": "\n".join(
            [
                _note(STUB_OUT_OF_CONTEXT, first),
                _note(STUB_OUT_OF_CONTEXT, second),
                "[Изображение #1] [Изображение #2]",
            ]
        ),
    }


def test_out_of_context_without_the_view_tool_is_the_nobody_note():
    """Подсказка про fs_view_image — только когда тул в наборе и есть кому смотреть."""
    att = _attachment(1)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1]", att)],
        audience=SEES_WITHOUT_TOOL,
        live_ids=frozenset(),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert (
        projected["content"] == _note(STUB_NOBODY, att) + "\n[Изображение #1]"
    )


def test_blind_request_model_with_eyes_gets_the_model_blind_note():
    att = _attachment(1)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1]", att)],
        audience=BLIND_WITH_EYES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert (
        projected["content"]
        == _note(STUB_MODEL_BLIND, att) + "\n[Изображение #1]"
    )


#: Модель запроса известна слепой (проба UNSUPPORTED или `vision: false`).
REJECTS_WITH_EYES = Audience(
    native=None, eyes=MODEL, tool_visible=True, blind_alias="deepseek"
)
REJECTS_NOBODY = Audience(
    native=None, eyes=None, tool_visible=False, blind_alias="deepseek"
)


@pytest.mark.parametrize("origin", [ORIGIN_USER, ORIGIN_TOOL])
def test_known_blind_model_with_the_view_tool_is_told_to_call_it(origin):
    """Картинки пользователя и тулов (в том числе MCP) — одна записка."""
    att = _attachment(1, origin=origin)
    [projected] = project_outbound(
        [_message("m1", "текст", att)],
        audience=REJECTS_WITH_EYES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected["content"] == (
        f"[{att.label} · {wh_text(att)} · this image was not sent with this "
        "request: the selected model deepseek does not accept images · call "
        f"fs_view_image path={att.path} — another model will describe it for "
        "you]\nтекст"
    )


@pytest.mark.parametrize("origin", [ORIGIN_USER, ORIGIN_TOOL])
def test_known_blind_model_without_the_view_tool_asks_the_user(origin):
    att = _attachment(1, origin=origin)
    [projected] = project_outbound(
        [_message("m1", "текст", att)],
        audience=REJECTS_NOBODY,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected["content"] == (
        f"[{att.label} · {wh_text(att)} · this image was not sent with this "
        "request: the selected model deepseek does not accept images · tell "
        "the user that a model with image support is needed, or ask them to "
        "describe it in words]\nтекст"
    )


def test_nobody_can_see_gets_the_nobody_note():
    att = _attachment(1)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1]", att)],
        audience=NOBODY,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert (
        projected["content"] == _note(STUB_NOBODY, att) + "\n[Изображение #1]"
    )


def test_live_image_with_unreadable_bytes_is_the_file_missing_note():
    att = _attachment(1)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1]", att)],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_unreadable,
    )
    assert (
        projected["content"]
        == _note(STUB_FILE_MISSING, att) + "\n[Изображение #1]"
    )


def test_deleted_image_file_goes_as_file_missing_note(tmp_path):
    """Review Focus №4: тред открыт заново, файл картинки удалён вручную — запрос
    уходит с запиской «file unavailable», исключения нет."""
    store = ImageStore(str(tmp_path))
    att = store.stage(
        NormalizedImage(
            data=PNG,
            mime="image/png",
            width=40,
            height=30,
            orig_width=40,
            orig_height=30,
        ),
        origin=ORIGIN_USER,
        label=user_label(1),
        source="clipboard",
    )
    os.remove(att.path)
    [projected] = project_outbound(
        [_message("m1", "[Изображение #1] что тут?", att)],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=lambda a: run_coro(store.aread_for_send(a)),
    )
    assert projected == {
        "role": "user",
        "id": "m1",
        "content": _note(STUB_FILE_MISSING, att)
        + "\n[Изображение #1] что тут?",
    }


def test_responses_form_is_a_string_in_the_same_message_shape():
    """В responses пикселей нет никогда (спека §20.5) — даже при «видящей»
    аудитории; прочие ключи сообщения уходят как были, ``attachments`` — нет."""
    att = _attachment(1)
    message = {
        **_message("m1", "[Изображение #1] что тут?", att),
        "type": "message",
        "display_content": "что тут?",
    }
    [projected] = project_outbound(
        [message],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode="responses",
        read_bytes=_read,
    )
    assert projected == {
        "type": "message",
        "role": "user",
        "id": "m1",
        "display_content": "что тут?",
        "content": _note(STUB_OUT_OF_CONTEXT, att)
        + "\n[Изображение #1] что тут?",
    }


def test_chat_form_drops_type_message_so_image_parts_reach_the_wire():
    """Форма ``type: "message"`` на проводе теряет нетекстовые части
    (``format_messages._canonical_to_chat_completions_message``)."""
    att = _attachment(1)
    message = {**_message("m1", "[Изображение #1]", att), "type": "message"}
    [projected] = project_outbound(
        [message],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert format_message_chat_completions_to_send_backend(projected) == {
        "role": "user",
        "content": [
            *_pixels(att),
            {"type": "text", "text": "[Изображение #1]"},
        ],
    }


def test_technical_message_with_a_tool_image_keeps_its_technical_text_last():
    att = _attachment(7, origin=ORIGIN_TOOL)
    technical = wrap_technical_user_content(
        TECH_LINE_TOOL.format(source=att.source, wh=wh_text(att))
    )
    message = {
        "role": "user",
        "id": "t1",
        "content": technical,
        "attachments": [att.to_dict()],
    }
    [projected] = project_outbound(
        [message],
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert projected == {
        "role": "user",
        "id": "t1",
        "content": [*_pixels(att), {"type": "text", "text": technical}],
    }


def test_input_messages_are_not_mutated():
    att = _attachment(1)
    messages = [_message("m1", "[Изображение #1]", att)]
    snapshot = copy.deepcopy(messages)
    project_outbound(
        messages,
        audience=SEES,
        live_ids=frozenset({att.id}),
        api_mode=CHAT,
        read_bytes=_read,
    )
    assert messages == snapshot


def test_request_carries_images_only_when_the_model_sees_and_images_are_live():
    live = frozenset({"0000000000000001"})
    assert request_carries_images(SEES, live) is True
    assert request_carries_images(SEES, frozenset()) is False
    assert request_carries_images(BLIND_WITH_EYES, live) is False
