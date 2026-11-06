"""Отказ вызова для ленты: кто отказал и почему (спека P3 §10).

Отказ — не сбой тула: действие остановили политика, режим, судья или
человек. Узнаётся по конверту отказа ядра (`error.type`, `error.reason`).
Исход вопроса подтверждения сильнее типа: гард после своего вопроса
оставляет свой тип, а причину берёт по исходу (`guard_refusal`). Тексты
строки `└` — одна таблица ниже.
"""

from __future__ import annotations

from typing import Any, Optional

from gc_core.message_policy.permission_denials import (
    ACTION_BLOCKED_TYPE,
    DECLINED_REASON,
    DECLINED_TYPE,
    OTHER_HOST_REDIRECT_TYPE,
    SESSION_DENIED_REASON,
    SESSION_DENIED_TYPE,
    TIMED_OUT_REASON,
    TIMED_OUT_TYPE,
    UNATTENDED_REASON,
    UNATTENDED_TYPE,
)

_DECLINED = "вы отклонили"
_SESSION_DENIED = "вы запретили в этой сессии"
_UNATTENDED = "нужно подтверждение — спросить некого"
_TIMED_OUT = "нет ответа на подтверждение"

#: Исход вопроса подтверждения — по причине отказа.
_BY_CONFIRM_REASON = {
    DECLINED_REASON: _DECLINED,
    SESSION_DENIED_REASON: _SESSION_DENIED,
    UNATTENDED_REASON: _UNATTENDED,
    TIMED_OUT_REASON: _TIMED_OUT,
}

#: Отказы с готовым текстом — по типу.
_FIXED = {
    DECLINED_TYPE: _DECLINED,
    "unsafe_command_cancelled_by_user": _DECLINED,
    SESSION_DENIED_TYPE: _SESSION_DENIED,
    UNATTENDED_TYPE: _UNATTENDED,
    TIMED_OUT_TYPE: _TIMED_OUT,
}

#: Запрет режима разрешений: что запрещено.
_MODE_DENIED = {
    "write_denied_by_mode": "запись запрещена",
    "read_denied_by_mode": "чтение запрещено",
    "exec_denied_by_mode": "запуск команд запрещён",
    "network_denied_by_mode": "сетевые запросы запрещены",
    "permission_denied_by_mode": "действие запрещено",
    OTHER_HOST_REDIRECT_TYPE: "переход на другой хост не выполнен",
}

_PATH_GUARD = "защита путей"

#: Кто отказал своими словами: причина — как есть.
_WHO = {
    ACTION_BLOCKED_TYPE: "отклонил судья auto",
    "unsafe_command_blocked": "модерация команд",
    "url_blocked_by_policy": "политика адресов",
    "write_guard_violation": _PATH_GUARD,
    "read_guard_violation": _PATH_GUARD,
    "protected_path_denied": _PATH_GUARD,
    "read_denied": _PATH_GUARD,
}


def refusal_text(error: Any) -> Optional[str]:
    """Строка `└` отказа: «кто: почему»; None — это не отказ."""
    if not isinstance(error, dict):
        return None
    kind = error.get("type")
    reason = error.get("reason")
    by_answer = (
        _BY_CONFIRM_REASON.get(reason) if isinstance(reason, str) else None
    )
    fixed = by_answer or _FIXED.get(kind)
    if fixed:
        return fixed
    if kind in _MODE_DENIED:
        mode = error.get("mode")
        who = f"режим {mode}" if mode else "режим"
        return f"{who}: {_MODE_DENIED[kind]}"
    who = _WHO.get(kind)
    if who is None:
        return None
    why = error.get("reason") or error.get("message") or error.get("path")
    return f"{who}: {why}" if why else who
