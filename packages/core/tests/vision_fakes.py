"""Explicitly confirmed routes for tests of behavior after visual admission."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

from gc_core.settings import runtime_of, settings_of
from gc_core.utils.connect_backend import build_call_headers
from gc_core.utils.llm_chat_client import snapshot_chat_completion
from gc_core.vision.audience import route_id
from gc_core.vision.probe import ProbeResult, ProbeStatus
from gc_core.vision.registry import process_vision_probes
from gc_core.vision.rejects import process_image_rejects
from gc_core.vision.route import vision_route_key


def clear_image_rejects() -> None:
    """Сбросить потолки процесса на месте — объект тот же: ссылки, взятые до
    сброса, видят пустой реестр."""
    registry = process_image_rejects()
    with registry._lock:
        registry._caps = {}


def route_key(ctx, *, url=None, model=None, resolved=None):
    resolved = resolved or ctx.model_router.resolve("main")
    if url is not None or model is not None:
        resolved = replace(
            resolved, url=url or resolved.url, model=model or resolved.model
        )
    proxy_ctx = SimpleNamespace(
        settings=settings_of(ctx),
        runtime=runtime_of(ctx),
        auth_connector=getattr(ctx, "auth_connector", None),
    )
    state = SimpleNamespace(state_id="test-thread", x_target=None)
    prepared = snapshot_chat_completion(
        proxy_ctx,
        state,
        resolved=resolved,
        headers=build_call_headers(
            state, proxy_ctx, resolved, api_mode="chat_completions"
        ),
    )
    return vision_route_key(prepared)


def confirm(key):
    async def yes():
        return ProbeResult(ProbeStatus.CONFIRMED, "code_read")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(
            asyncio.run,
            process_vision_probes().ensure(
                key, turn_key="test-warmup", run=yes
            ),
        ).result(timeout=3)


def warm_routes(ctx):
    routes = {}
    if getattr(ctx, "model_router", None) is None:
        return routes
    for consumer in ["main", "vision"]:
        try:
            resolved = ctx.model_router.resolve(consumer)
        except (ValueError, RuntimeError):
            continue
        key = route_key(ctx, resolved=resolved)
        confirm(key)
        routes[route_id(resolved)] = key
    return routes


def unsupported(key):
    result = process_vision_probes().peek(key)
    return result is not None and result.status is ProbeStatus.UNSUPPORTED
