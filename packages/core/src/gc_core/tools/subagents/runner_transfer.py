"""Transfer the ordinary runner's parent-side effects across an execution port.

The runner remains the source of child policy and accounting. This module only
captures its acknowledged state and merges the change since the last snapshot.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from gc_core.execution.contracts import ExecutionFailure
from gc_core.usage.account import UsageAccount


@dataclass(frozen=True)
class SubagentTransferState:
    shared: dict[str, Any]
    usage: dict[str, Any]
    tool_calls: int
    run_permissions: dict[str, bool]
    circuit_breaker: dict[str, str]


def capture_subagent_transfer(state, ctx) -> SubagentTransferState:
    permissions = getattr(ctx, "permission_store", None)
    router = getattr(ctx, "model_router", None)
    shared = asdict(state.shared)
    shared["fetch_url_cache"] = [
        [key, value] for key, value in shared["fetch_url_cache"].items()
    ]
    return SubagentTransferState(
        shared,
        state.usage.to_meta(),
        state.tool_calls,
        permissions.export_run_rules() if permissions is not None else {},
        router.breaker.snapshot() if router is not None else {},
    )


_MISSING = object()

#: Счётчики общей сессии, которые параллельные агенты меняют одновременно:
#: складываются по приросту, как `tool_calls`, а не конфликтуют.
_ADDITIVE_COUNTERS = frozenset(
    {"shared.judge_denials_in_a_row", "shared.judge_denials_total"}
)


def _merge_changes(before, after, current, path):
    if before == after:
        return deepcopy(current) if current is not _MISSING else _MISSING
    if path in _ADDITIVE_COUNTERS and all(
        isinstance(value, int) for value in (before, after, current)
    ):
        return _merge_counter(before, after, current)
    # Словари — поключево: совпадение целиком скрыло бы счётчики, случайно
    # сравнявшиеся у двух агентов.
    dicts = all(isinstance(value, dict) for value in (before, after, current))
    if not dicts and current in (before, after):
        return deepcopy(after) if after is not _MISSING else _MISSING
    if dicts:
        return _merge_dicts(before, after, current, path)
    raise ExecutionFailure(f"Concurrent subagent state change: {path}")


def _merge_dicts(before: dict, after: dict, current: dict, path: str) -> dict:
    result = deepcopy(current)
    for key in dict.fromkeys([*before, *after]):
        merged = _merge_changes(
            before.get(key, _MISSING),
            after.get(key, _MISSING),
            current.get(key, _MISSING),
            f"{path}.{key}",
        )
        if merged is _MISSING:
            result.pop(key, None)
        else:
            result[key] = merged
    return result


def _merge_counter(before: int, after: int, current: int) -> int:
    """Прирост ребёнка прибавляется к текущему счёту — и при равных значениях
    (0→1 у двоих — это 2). Счёт у ребёнка упал — значит, был сброс (судья
    разрешил, общий порог сработал): сброс побеждает, берётся счёт ребёнка.

    Счёт при параллельных детях приблизителен (решение владельца): события
    разных детей не упорядочены, и отказы соседа рядом со сбросом теряются.
    Признак паузы сливается точно — он и защищает."""
    if after < before:
        return after
    return current + (after - before)


def _merge_breaker(before: dict, after: dict, current: dict) -> dict:
    """Предохранитель: изменение ребёнка побеждает, причины не конфликтуют.

    Ключ, исчезнувший у ребёнка, удаляется, только если родитель не успел
    поменять его причину."""
    result = dict(current)
    for alias in dict.fromkeys([*before, *after]):
        if before.get(alias) == after.get(alias):
            continue
        if alias in after:
            result[alias] = after[alias]
        elif result.get(alias) == before[alias]:
            del result[alias]
    return result


def apply_subagent_transfer(state, ctx, before, after) -> None:
    """Apply only new child effects, preserving concurrent parent changes.

    All shared/permission/breaker conflicts are validated before mutation. Maps
    are updated in place so PermissionStore keeps its session-rules reference.
    Re-delivery of a checkpoint is handled by the caller's sequence receipt.
    """
    current = capture_subagent_transfer(state, ctx)
    shared = _merge_changes(
        before.shared, after.shared, current.shared, "shared"
    )
    permissions = _merge_changes(
        before.run_permissions,
        after.run_permissions,
        current.run_permissions,
        "run_permissions",
    )
    breaker = _merge_breaker(
        before.circuit_breaker,
        after.circuit_breaker,
        current.circuit_breaker,
    )
    calls = after.tool_calls - before.tool_calls
    if calls < 0:
        raise ExecutionFailure("Subagent tool counter moved backwards")
    for name, value in shared.items():
        existing = getattr(state.shared, name)
        if isinstance(existing, dict):
            existing.clear()
            existing.update(deepcopy(value))
        elif isinstance(existing, list):
            existing[:] = deepcopy(value)
        else:
            setattr(state.shared, name, deepcopy(value))
    state.usage.merge(
        UsageAccount.from_meta(after.usage).delta_from(
            UsageAccount.from_meta(before.usage)
        )
    )
    state.tool_calls += calls
    store = getattr(ctx, "permission_store", None)
    if store is not None:
        store.replace_run_rules(permissions)
    router = getattr(ctx, "model_router", None)
    if router is not None:
        router.breaker.replace_from(breaker)
