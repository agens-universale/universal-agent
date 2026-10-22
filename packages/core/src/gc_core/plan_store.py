"""План режима plan: статусы, подписи, лимит, форма блока в состоянии сессии.

Канон плана — ``SharedStateData.plan`` (и мета треда): отсюда его читают
одобрение, компакция, восстановление сессии. Листовой модуль: его импортирует
слой хранения, поэтому зависимостей от защит и фронтендов здесь нет.
"""

from __future__ import annotations

from typing import Any, Mapping

from gc_core.execution.environment import utc_now

#: Потолок длины плана. Длиннее — тул просит сократить: план читает человек.
PLAN_MAX_CHARS = 16_000

PLAN_STATUS_PROPOSED = "proposed"
PLAN_STATUS_APPROVED = "approved"
PLAN_STATUS_REWORKING = "reworking"
PLAN_STATUSES = frozenset(
    {PLAN_STATUS_PROPOSED, PLAN_STATUS_APPROVED, PLAN_STATUS_REWORKING}
)

#: Подписи статусов для человека — рядом с самими статусами: их читают и
#: фронтенды, и свод компакции. Сырые значения в интерфейс не выводятся.
PLAN_STATUS_LABELS = {
    PLAN_STATUS_PROPOSED: "предложен",
    PLAN_STATUS_REWORKING: "в доработке",
    PLAN_STATUS_APPROVED: "утверждён",
}


def now_stamp() -> str:
    """Штамп времени плана. Часы — из окружения исполнения: под Temporal код
    плана идёт в workflow, где прямое чтение часов запрещено."""
    return utc_now().isoformat(timespec="seconds")


def new_plan_block(text: str) -> dict[str, Any]:
    """Блок только что предложенного плана."""
    return {
        "text": text.strip(),
        "status": PLAN_STATUS_PROPOSED,
        "approved_mode": None,
        "updated_at": now_stamp(),
    }


def plan_is_accepted(plan: Any, text: str | None = None) -> bool:
    """План принят человеком. С ``text`` — принят именно этот текст плана
    (фронтенд отличает действующий план от прежних, заменённых)."""
    block = normalize_plan_block(plan)
    if block is None or block["status"] != PLAN_STATUS_APPROVED:
        return False
    return text is None or block["text"].strip() == text.strip()


def plan_status_label(plan: Any) -> str:
    """Подпись статуса плана для человека; пусто — плана нет."""
    block = normalize_plan_block(plan)
    return PLAN_STATUS_LABELS[block["status"]] if block else ""


def plan_accepted_mode(plan: Any) -> str | None:
    """Режим, в котором план утверждён; None — не утверждён."""
    block = normalize_plan_block(plan)
    if block is None or block["status"] != PLAN_STATUS_APPROVED:
        return None
    return block["approved_mode"]


def plan_awaits_decision(plan: Any) -> bool:
    """План есть и ждёт решения человека (предложен или в доработке)."""
    block = normalize_plan_block(plan)
    return block is not None and block["status"] != PLAN_STATUS_APPROVED


def normalize_plan_block(raw: Any) -> dict[str, Any] | None:
    """Валидный блок плана или None: мусор в мете не роняет загрузку сессии."""
    if not isinstance(raw, Mapping):
        return None
    text = str(raw.get("text") or "")
    status = str(raw.get("status") or "")
    if not text.strip() or status not in PLAN_STATUSES:
        return None
    approved = raw.get("approved_mode")
    return {
        "text": text,
        "status": status,
        "approved_mode": (
            approved
            if isinstance(approved, str) and approved.strip()
            else None
        ),
        "updated_at": str(raw.get("updated_at") or ""),
    }
