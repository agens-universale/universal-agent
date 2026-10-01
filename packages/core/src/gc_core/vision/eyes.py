"""«Глаза» — описание изображения моделью для зрения (спека §8).

Зовёт тул ``fs_view_image``, когда модель запроса картинку не видит. Вызов идёт
служебной воронкой ``llm_chat_client`` (потребитель ``vision``: не назначен —
активная модель; категория расхода ``vision``) — одна часть ``image_url`` в
user-сообщении. Исключения бэкенда пробрасываются как есть: «модель не видит
картинок» это или нет, решает вызывающий (``is_image_rejection_error``).
"""

from __future__ import annotations

from functools import partial
from typing import Any

from gc_core.attachments.model import Attachment
from gc_core.attachments.texts import EYES_SYSTEM_PROMPT
from gc_core.intent_log.record import ACTION_USER_ASK
from gc_core.utils.data_urls import data_url
from gc_core.utils.llm_chat_client import arequest_chat_completion_text
from gc_core.vision.service import arequire_confirmed_route

EYES_MAX_TOKENS = 1500
EYES_TEMPERATURE = 0.1
TASK_CONTEXT_MAX_CHARS = 1500
# Обёртки частей запроса к «глазам» (для модели, EN — конвенция проекта).
EYES_TASK_CONTEXT = "<task_context>\n{task_context}\n</task_context>"
EYES_QUESTION = "<question>\n{question}\n</question>"


def task_context_of(state: Any) -> str:
    """Текущая просьба человека — реплики открытого хода, не длиннее 1500 символов.

    Реплики хода копит ``state.current_user_intent`` (записи ``user_ask``: старт хода
    и сообщения посреди запуска; у субагента — его задание).
    """
    asks = [
        str(record.content).strip()
        for record in getattr(state, "current_user_intent", None) or []
        if getattr(record, "action", None) == ACTION_USER_ASK
    ]
    return "\n".join(ask for ask in asks if ask)[:TASK_CONTEXT_MAX_CHARS]


async def adescribe_image(
    ctx: Any,
    state: Any,
    attachment: Attachment,
    *,
    question: str,
    task_context: str,
) -> str:
    """Описание картинки текстом; пустая строка — модель ничего не ответила."""
    data = ctx.image_store.read_for_send(attachment)
    if data is None:
        raise FileNotFoundError(attachment.path)
    parts: list[dict[str, Any]] = []
    if task_context:
        parts.append(
            {
                "type": "text",
                "text": EYES_TASK_CONTEXT.format(task_context=task_context),
            }
        )
    parts.append(
        {"type": "text", "text": EYES_QUESTION.format(question=question)}
    )
    parts.append(
        {
            "type": "image_url",
            "image_url": {"url": data_url(attachment.mime, data)},
        }
    )
    return await arequest_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=[
            {"role": "system", "content": EYES_SYSTEM_PROMPT},
            {"role": "user", "content": parts},
        ],
        max_tokens=EYES_MAX_TOKENS,
        consumer="vision",
        request_label="vision",
        payload_overrides={"temperature": EYES_TEMPERATURE},
        allow_images=True,
        before_send=partial(arequire_confirmed_route, ctx, state),
    )
