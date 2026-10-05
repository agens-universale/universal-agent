from dataclasses import replace

from gc_core.utils.llm_chat_client import (
    aprepare_chat_completion,
)
from gc_core.vision.route import vision_route_key
from test_llm_chat_client_once import context, model, state


async def test_automatic_ids_and_aliases_share_route():
    ctx, st = context(), state()
    first = await aprepare_chat_completion(ctx, st, resolved=model())
    st.state_id = "other-thread"
    second = await aprepare_chat_completion(
        ctx, st, resolved=replace(model(), alias="other")
    )
    assert vision_route_key(first) == vision_route_key(second)
    assert vision_route_key(first) == vision_route_key(
        replace(
            first, headers={k.upper(): v for k, v in first.headers.items()}
        )
    )
    assert "secret" not in repr(vision_route_key(first))


async def test_explicit_routing_and_access_change_route():
    ctx, st = context(), state()
    first = await aprepare_chat_completion(ctx, st, resolved=model())
    for headers in (
        {"Authorization": "Bearer different"},
        {"x-target": "other"},
        {"x-session-id": "explicit"},
    ):
        second = await aprepare_chat_completion(
            ctx, st, resolved=model(chat_completion_headers=headers)
        )
        assert vision_route_key(first) != vision_route_key(second)
    second = await aprepare_chat_completion(
        ctx,
        st,
        resolved=model(
            chat_completion_headers={"x-session-id": "explicit-two"}
        ),
    )
    third = await aprepare_chat_completion(
        ctx,
        st,
        resolved=model(chat_completion_headers={"x-session-id": "explicit"}),
    )
    assert vision_route_key(second) != vision_route_key(third)


async def test_duplicate_header_order_is_part_of_effective_route():
    prepared = await aprepare_chat_completion(
        context(), state(), resolved=model()
    )
    first = replace(
        prepared, headers={"X-Target": "text", "x-target": "vision"}
    )
    second = replace(
        prepared, headers={"X-Target": "vision", "x-target": "text"}
    )
    assert vision_route_key(first) != vision_route_key(second)
