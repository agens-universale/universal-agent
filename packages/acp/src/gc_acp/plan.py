"""Plan / title / usage → session/update (пакет E)."""

from __future__ import annotations

from typing import Any

from gc_core.persistence.schema import TODO_STATUSES, normalize_session_todo
from gc_core.thread_store import load_thread_meta
from gc_core.usage.account import COST_EPSILON, MONEY_FIELD, UsageAccount

from gc_acp.session_index import ACP_SYSTEM

_PLAN = "plan"
_INFO = "session_info_update"
_USAGE = "usage_update"
_PRIORITY = "medium"
_STATUSES = frozenset(TODO_STATUSES)


def entries_from_todos(raw: Any) -> list[dict[str, str]]:
    """Пункты ACP Plan. Неизвестный status → pending. Пустой content — пропуск."""
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if content == "":
            continue
        status = item.get("status")
        if status not in _STATUSES:
            status = "pending"
        entries.append(
            {
                "content": content,
                "status": str(status),
                "priority": _PRIORITY,
            }
        )
    return entries


def plan_update(todos: Any) -> dict:
    return {"sessionUpdate": _PLAN, "entries": entries_from_todos(todos)}


def session_info_update(title: str) -> dict:
    return {"sessionUpdate": _INFO, "title": title}


def usage_update(
    *,
    used: int | None,
    size: int | None,
    account: UsageAccount | None = None,
) -> dict | None:
    """usage_update или None, если нет used/size."""
    if (
        not isinstance(used, int)
        or isinstance(used, bool)
        or used < 0
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
    ):
        return None
    body: dict[str, Any] = {
        "sessionUpdate": _USAGE,
        "used": used,
        "size": size,
    }
    cost = _single_cost(account)
    if cost is not None:
        body["cost"] = cost
    return body


def context_window_size(settings: Any, choice: str) -> int | None:
    """Размер окна текущей модели, если известен. Иначе None.

    Каталог провайдера (``model_context_window``) в ACP-процессе обычно
    недоступен без probe — тогда usage_update не шлём.
    """
    del settings, choice
    return None


def todos_from_meta(threads_dir: str, session_id: str) -> list[dict[str, str]]:
    meta = _meta(threads_dir, session_id)
    shared = meta.get("shared")
    if not isinstance(shared, dict):
        return []
    return normalize_session_todo(shared.get("session_todo"))


def used_tokens_from_meta(threads_dir: str, session_id: str) -> int | None:
    meta = _meta(threads_dir, session_id)
    value = meta.get("last_main_prompt_tokens")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def usage_account_from_meta(
    threads_dir: str, session_id: str
) -> UsageAccount | None:
    meta = _meta(threads_dir, session_id)
    raw = meta.get("token_usage")
    if raw is None:
        return None
    return UsageAccount.from_meta(raw)


def agent_state(agent: Any) -> Any | None:
    executor = getattr(agent, "executor", None)
    get_state = getattr(executor, "get_state", None)
    if get_state is None:
        return None
    try:
        return get_state()
    except Exception:
        return None


def todos_from_agent(agent: Any) -> list[dict[str, str]]:
    state = agent_state(agent)
    if state is None:
        return []
    return normalize_session_todo(getattr(state, "session_todo", None))


def usage_from_agent(agent: Any, *, settings: Any, choice: str) -> dict | None:
    state = agent_state(agent)
    if state is None:
        return None
    used = getattr(state, "last_main_prompt_tokens", None)
    size = context_window_size(settings, choice)
    account = getattr(state, "usage", None)
    if not isinstance(account, UsageAccount):
        account = None
    return usage_update(used=used, size=size, account=account)


def usage_from_meta(
    threads_dir: str,
    session_id: str,
    *,
    settings: Any,
    choice: str,
) -> dict | None:
    used = used_tokens_from_meta(threads_dir, session_id)
    size = context_window_size(settings, choice)
    account = usage_account_from_meta(threads_dir, session_id)
    return usage_update(used=used, size=size, account=account)


def _single_cost(account: UsageAccount | None) -> dict | None:
    if account is None:
        return None
    money = account.totals().get(MONEY_FIELD) or {}
    if not isinstance(money, dict) or len(money) != 1:
        return None
    currency, amount = next(iter(money.items()))
    if not isinstance(currency, str) or currency == "":
        return None
    if not isinstance(amount, (int, float)) or amount < COST_EPSILON:
        return None
    return {"amount": float(amount), "currency": currency}


def _meta(threads_dir: str, session_id: str) -> dict:
    try:
        meta = load_thread_meta(threads_dir, ACP_SYSTEM, session_id)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}
