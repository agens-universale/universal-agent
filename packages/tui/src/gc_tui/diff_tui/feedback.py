"""Technical user message после undo в Diff-TUI (для agent thread).

UX-строки для DiffScreen / trace — отдельно от technical message агенту.
"""

from __future__ import annotations

from typing import Optional, Sequence

from gc_core.message_policy.user_message_policy import TECHNICAL_TYPE_TECHNICAL, wrap_technical_user_content
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func


def files_word(n: int) -> str:
    """Русское склонение «файл/файла/файлов»."""
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 19:
        return "файлов"
    if n1 == 1:
        return "файл"
    if 2 <= n1 <= 4:
        return "файла"
    return "файлов"


def format_undo_ack(undone_keys: Sequence[str]) -> str | None:
    """Короткий ack для DiffScreen / trace после undo."""
    keys = [k for k in undone_keys if k]
    if not keys:
        return None
    if len(keys) == 1:
        return f"Отменено: {keys[0]}"
    return f"Отменено {len(keys)} {files_word(len(keys))}"


def format_undo_noop_ack(key: str, reason: str) -> str | None:
    """Ack когда undo не применился (редкий fallback)."""
    if not key or not reason:
        return None
    return None


def format_approve_ack(count: int, key: str | None = None) -> str | None:
    """Короткий ack после approve (одного или all).

    Без слова «staged» — для cache-backed отличие даёт маркер ``i`` /
    двуслойный bulk-ack.
    """
    if count <= 0:
        return None
    if count == 1 and key:
        return f"Принято: {key}"
    return f"Принято {count} {files_word(count)}"


def _dual_layer_parts(git_count: int, cache_count: int) -> list[str]:
    parts: list[str] = []
    if git_count > 0:
        parts.append(f"git: {git_count}")
    if cache_count > 0:
        parts.append(f"cache: {cache_count}")
    return parts


def format_bulk_approve_ack(git_count: int, cache_count: int) -> Optional[str]:
    """Двуслойный ack для approve all: «Принято: git: N; cache: M»."""
    parts = _dual_layer_parts(git_count, cache_count)
    if not parts:
        return None
    return "Принято: " + "; ".join(parts)


def format_bulk_undo_ack(git_count: int, cache_count: int) -> Optional[str]:
    """Двуслойный ack для undo all: «Отменено: git: N; cache: M»."""
    parts = _dual_layer_parts(git_count, cache_count)
    if not parts:
        return None
    return "Отменено: " + "; ".join(parts)


_PENDING_CTA_PREVIEW = 5


def format_pending_cta(paths: Sequence[str]) -> str | None:
    """Post-turn CTA в трейсе: до 5 путей + ``F3/diff``.

    Если pending больше лимита — список режется и хвост
    ``(5 из N) F3/diff``; иначе просто ``F3/diff``.
    """
    keys = [p for p in paths if p]
    n = len(keys)
    if n <= 0:
        return None
    shown = keys[:_PENDING_CTA_PREVIEW]
    lines = list(shown)
    if n > _PENDING_CTA_PREVIEW:
        lines.append(f"({_PENDING_CTA_PREVIEW} из {n}) F3/diff")
    else:
        lines.append("F3/diff")
    return "\n".join(lines)


def build_undo_feedback_body(
    restored_keys: Sequence[str] = (),
    deleted_keys: Sequence[str] = (),
) -> str | None:
    restored = [k for k in restored_keys if k]
    deleted = [k for k in deleted_keys if k]
    if not restored and not deleted:
        return None

    parts = ["User reverted pending file changes via Diff-UI undo."]
    if restored:
        lines = "\n".join(f"- {k}" for k in restored)
        parts.append(f"Changes reverted — re-read these files from disk before assuming their contents:\n{lines}")
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
