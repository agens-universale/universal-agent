"""Technical user message агенту после отката правок (§7).

Откатили файлы — модель обязана узнать, что её представление о содержимом
устарело, иначе следующая правка ляжет поверх выдуманного состояния. Тексты
достались от прежней механики отката как есть: она умерла, а формулировки для
агента уже обкатаны и менять их незачем.

Слой данных: без UI (сообщение адресовано модели, не пользователю).
"""

from __future__ import annotations

from typing import Sequence

from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_TECHNICAL,
    wrap_technical_user_content,
)
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)


def build_undo_feedback_body(
    restored_keys: Sequence[str] = (),
    deleted_keys: Sequence[str] = (),
) -> str | None:
    restored = [k for k in restored_keys if k]
    deleted = [k for k in deleted_keys if k]
    if not restored and not deleted:
        return None

    parts = ["User reverted file changes via Diff-UI undo."]
    if restored:
        lines = "\n".join(f"- {k}" for k in restored)
        parts.append(
            "Changes reverted — re-read these files from disk "
            "before assuming their contents:\n"
            f"{lines}"
        )
    if deleted:
        lines = "\n".join(f"- {k}" for k in deleted)
        parts.append(f"Deleted previously created files:\n{lines}")
    return "\n\n".join(parts) + "\n"


def build_undo_feedback_message(
    restored_keys: Sequence[str] = (),
    deleted_keys: Sequence[str] = (),
) -> dict | None:
    """``role:user`` technical message или ``None``, если ключей нет."""
    body = build_undo_feedback_body(restored_keys, deleted_keys)
    if body is None:
        return None
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            body,
            technical_type=TECHNICAL_TYPE_TECHNICAL,
        ),
        "id": generate_message_id_func("m_diff_undo"),
    }
