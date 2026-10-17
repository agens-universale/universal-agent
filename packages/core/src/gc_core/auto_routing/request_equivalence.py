"""Routing identity facts without worker-only request configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gc_core.auto_routing.policy import LEVEL_ORDER, START_LEVEL

if TYPE_CHECKING:
    from gc_core.model_router import ResolvedModel


@dataclass(frozen=True)
class MainRequestRef:
    alias: str | None
    model: str


@dataclass(frozen=True)
class PreparedRequestEquivalence:
    context_id: str
    routes: tuple[MainRequestRef | None, ...]
    groups: tuple[int | None, ...]


def resolve_level_requests(source: Any) -> dict[str, ResolvedModel]:
    from gc_core.model_router import ModelRouter

    router = ModelRouter(source, level_provider=lambda: START_LEVEL)
    kits = {}
    for level in LEVEL_ORDER:
        try:
            kits[level] = router.resolve_level(level, check_breaker=False)
        except ValueError:
            continue
    return kits


def _ref(resolved: ResolvedModel) -> MainRequestRef:
    return MainRequestRef(resolved.alias, resolved.model)


def prepare_request_equivalence(
    source: Any, *, context_id: str
) -> PreparedRequestEquivalence:
    from gc_core.utils.connect_backend import same_main_request

    if not isinstance(context_id, str) or not context_id.strip():
        raise ValueError(
            "Prepared request equivalence needs a context identity"
        )
    kits = resolve_level_requests(source)
    representatives, refs, groups = [], [], []
    for level in LEVEL_ORDER:
        resolved = kits.get(level)
        if resolved is None:
            refs.append(None)
            groups.append(None)
            continue
        group = next(
            (
                i
                for i, other in enumerate(representatives)
                if same_main_request(source, resolved, other)
            ),
            None,
        )
        if group is None:
            group = len(representatives)
            representatives.append(resolved)
        refs.append(_ref(resolved))
        groups.append(group)
    return PreparedRequestEquivalence(context_id, tuple(refs), tuple(groups))


def validate_request_equivalence(
    source: Any,
    facts: PreparedRequestEquivalence,
    *,
    context_id: str,
) -> None:
    if (
        not isinstance(facts, PreparedRequestEquivalence)
        or not isinstance(context_id, str)
        or not context_id.strip()
        or facts.context_id != context_id
        or len(facts.routes) != len(LEVEL_ORDER)
        or len(facts.groups) != len(LEVEL_ORDER)
    ):
        raise ValueError("Invalid prepared request equivalence scope")
    kits = resolve_level_requests(source)
    known = {}
    for level, ref, group in zip(LEVEL_ORDER, facts.routes, facts.groups):
        resolved = kits.get(level)
        if resolved is None:
            if ref is not None or group is not None:
                raise ValueError(
                    "Prepared request equivalence has an unknown route"
                )
            continue
        if (
            ref != _ref(resolved)
            or type(group) is not int
            or not 0 <= group < len(LEVEL_ORDER)
        ):
            raise ValueError(
                "Prepared request equivalence differs from configured routes"
            )
        if ref in known and known[ref] != group:
            raise ValueError(
                "One prepared route has conflicting equivalence groups"
            )
        known[ref] = group


def same_routing_request(
    source: Any,
    first: ResolvedModel,
    second: ResolvedModel,
    *,
    first_level: str,
    second_level: str,
    legacy: bool = False,
) -> bool:
    if legacy:
        return first.same_call_kit(second)
    facts = getattr(source, "prepared_request_equivalence", None)
    if facts is None:
        from gc_core.utils.connect_backend import same_main_request

        return same_main_request(source, first, second)
    validate_request_equivalence(
        source, facts, context_id=getattr(source, "run_id", "")
    )
    a, b = LEVEL_ORDER.index(first_level), LEVEL_ORDER.index(second_level)
    if facts.routes[a] != _ref(first) or facts.routes[b] != _ref(second):
        raise ValueError(
            "Prepared request equivalence does not describe this call"
        )
    return facts.groups[a] == facts.groups[b]
