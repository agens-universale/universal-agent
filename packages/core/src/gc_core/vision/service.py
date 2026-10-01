"""Lazy admission of an effective route before sending user pixels."""

from gc_core.utils.llm_chat_client import PreparedChatCompletion
from gc_core.vision.probe import ProbeResult, aprobe_route
from gc_core.vision.registry import process_vision_probes
from gc_core.vision.route import vision_route_key


async def aensure_route(
    ctx, state, prepared: PreparedChatCompletion
) -> ProbeResult:
    if not state.vision_probe_turn:
        raise RuntimeError("vision probe requires a root user turn")
    return await process_vision_probes().ensure(
        vision_route_key(prepared),
        turn_key=state.vision_probe_turn,
        run=lambda: aprobe_route(ctx, state, prepared),
    )
