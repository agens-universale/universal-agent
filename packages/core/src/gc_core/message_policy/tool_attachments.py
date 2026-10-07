"""Картинки батча — одним техническим user-сообщением в конце батча (спека §4, §7.1).

Tool-сообщение chat/completions картинку не несёт, а шлюзы responses теряют
function_call_output с картинкой (спека §2, решение 6). Поэтому картинки из тулов
(``fs_view_image``) и из ответа на ``turn_ask_human`` едут отдельным сообщением.
Байтов в нём нет: ``attachments`` — ссылки, картинку на провод ставит проекция
(``message_policy/attachments_outbound``); ``content`` называет каждую картинку
строкой — модель видит, откуда она.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from gc_core.attachments.model import ORIGIN_USER, Attachment, wh_text
from gc_core.attachments.texts import TECH_LINE_ASK, TECH_LINE_TOOL
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_TECHNICAL,
    wrap_technical_user_content,
)


def _line(att: Attachment) -> str:
    if att.origin == ORIGIN_USER:
        return TECH_LINE_ASK.format(label=att.label, wh=wh_text(att))
    return TECH_LINE_TOOL.format(source=att.source, wh=wh_text(att))


def build_tool_attachments_message(
    pending: Sequence[Mapping[str, Any]], *, message_id: str
) -> dict[str, Any]:
    """Одно техническое сообщение на батч; вложения уникальны по ``id`` (первое вхождение).

    Форма ``{"role": "user", …}`` без ``type`` — её ждёт проекция (спека §5).
    ``id`` обязателен: по нему дедуплицируются state и тред.
    """
    unique: dict[str, Attachment] = {}
    for raw in pending:
        att = Attachment.from_dict(raw)
        unique.setdefault(att.id, att)
    atts = list(unique.values())
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            "\n".join(_line(att) for att in atts),
            technical_type=TECHNICAL_TYPE_TECHNICAL,
        ),
        "id": message_id,
        "attachments": [att.to_dict() for att in atts],
    }
