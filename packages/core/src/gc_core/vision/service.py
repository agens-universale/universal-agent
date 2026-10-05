"""Lazy admission of an effective route before sending user pixels."""

from gc_core.utils.llm_chat_client import PreparedChatCompletion
from gc_core.vision.audience import _declared_blind, route_id
from gc_core.vision.notes import note_probe
from gc_core.vision.probe import ProbeResult, ProbeStatus, aprobe_route
from gc_core.vision.registry import process_vision_probes
from gc_core.vision.route import vision_route_key


async def aensure_route(
    ctx, state, prepared: PreparedChatCompletion
) -> ProbeResult:
    if not state.vision_probe_turn:
        raise RuntimeError("vision probe requires a root user turn")
    key = vision_route_key(prepared)
    routes = getattr(state, "vision_routes", None)
    if routes is None:
        routes = state.vision_routes = {}
    routes[route_id(prepared.resolved)] = key
    if _declared_blind(
        prepared.resolved, getattr(ctx, "config_dict", {}) or {}
    ):
        return ProbeResult(ProbeStatus.BLOCKED, "vision_disabled")
    result = await process_vision_probes().ensure(
        key,
        turn_key=state.vision_probe_turn,
        run=lambda: aprobe_route(ctx, state, prepared),
    )

    if result.status is not ProbeStatus.CONFIRMED:
        note_probe(ctx, state, key, prepared.resolved, result)
    return result


class VisionNotConfirmed(Exception):
    def __init__(self, result: ProbeResult):
        self.result = result
        super().__init__(result.reason)


async def arequire_confirmed_route(
    ctx, state, prepared: PreparedChatCompletion
) -> None:
    result = await aensure_route(ctx, state, prepared)
    if result.status is not ProbeStatus.CONFIRMED:
        raise VisionNotConfirmed(result)


async def aensure_view_audience(ctx, state):
    from gc_core.utils.llm_chat_client import aprepare_chat_completion
    from gc_core.vision.audience import (
        _resolve_eyes,
        compute_audience,
        view_tool_in_set,
    )

    router = getattr(ctx, "model_router", None)
    if router is None:
        return getattr(state, "vision_audience", None)
    cfg = getattr(ctx, "config_dict", {}) or {}
    level = getattr(state, "turn_level", None)
    main = (
        router.resolve_level(level)
        if level and not router.is_subagent_child
        else router.resolve("main")
    )
    mode = cfg.get("backend_api_mode", "responses")
    candidates = [main] if mode == "chat_completions" else []
    eyes = _resolve_eyes(router)
    if eyes is not None and route_id(eyes) not in {
        route_id(m) for m in candidates
    }:
        candidates.append(eyes)
    for candidate in candidates:
        if _declared_blind(candidate, cfg):
            continue
        prepared = await aprepare_chat_completion(
            ctx, state, resolved=candidate
        )
        result = await aensure_route(ctx, state, prepared)
        if result.status is ProbeStatus.CONFIRMED:
            break
    audience = compute_audience(
        router,
        request_resolved=main,
        api_mode=mode,
        tool_in_set=view_tool_in_set(getattr(state, "tools", []) or []),
        config=cfg,
        routes=getattr(state, "vision_routes", {}),
        turn_key=getattr(state, "vision_probe_turn", None),
    )
    state.vision_audience = audience
    return audience
