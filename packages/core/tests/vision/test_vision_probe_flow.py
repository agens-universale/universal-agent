import base64
import json

import httpx
import pytest
from gc_core import processing_responses as pr
from gc_core.usage.account import UsageAccount
from gc_core.vision import probe
from gc_core.vision.probe import make_challenge
from test_image_rejection_retry import OK, _cfg, _ctx, _state, _user_with_image
from transport_fakes import install_transport


def pngs(request):
    return [
        base64.b64decode(part["image_url"]["url"].split(",")[1])
        for message in json.loads(request.content).get("messages", [])
        for part in message.get("content", [])
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


@pytest.mark.parametrize(
    "answer,finish,expected_probes",
    [("correct", "stop", 1), ("wrong", "stop", 2), ("", "length", 1)],
)
async def test_native_pixels_require_successful_probe(
    tmp_path, monkeypatch, answer, finish, expected_probes
):
    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)

    def handler(request):
        if pngs(request) == [challenge.png]:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": challenge.code
                                if answer == "correct"
                                else answer
                            },
                            "finish_reason": finish,
                        }
                    ]
                },
            )
        return httpx.Response(200, json=OK)

    spy = install_transport(monkeypatch, handler)
    ctx = _ctx(tmp_path, _cfg(), [])
    att, message = _user_with_image(ctx.image_store)
    st = _state([message])
    st.vision_probe_turn = "root-one"
    st.usage = UsageAccount()
    await pr._arequest_backend_canonical([message], st, ctx)
    st.vision_probe_turn = "root-two"
    await pr._arequest_backend_canonical([message], st, ctx)
    assert (
        st.usage.by_category["vision"]["missing_usage_calls"]
        == expected_probes
    )
    assert len(spy.requests) == expected_probes + 2
    assert all(
        pngs(r) == [challenge.png] for r in spy.requests[:expected_probes]
    )
    expected = (
        [ctx.image_store.read_for_send(att)] if answer == "correct" else []
    )
    assert all(pngs(r) == expected for r in spy.requests[expected_probes:])


@pytest.mark.parametrize("image,disabled", [(False, False), (True, True)])
async def test_no_probe_without_need_or_when_disabled(
    tmp_path, monkeypatch, image, disabled
):
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=OK)
    )
    ctx = _ctx(
        tmp_path, _cfg(backend_vision=False) if disabled else _cfg(), []
    )
    _, message = _user_with_image(ctx.image_store)
    if not image:
        message.pop("attachments")
    st = _state([message])
    st.vision_probe_turn = "root"
    await pr._arequest_backend_canonical([message], st, ctx)
    assert len(spy.requests) == 1 and not pngs(spy.requests[0])


async def test_project_file_is_checked_lazily_before_attachment(
    tmp_path, monkeypatch
):
    import io

    from gc_core.attachments.normalize import normalize_image
    from gc_core.attachments.store import ImageStore, image_id
    from gc_core.model_router import ModelRouter
    from gc_core.tools.fs.view_image import ViewImage
    from gc_core.vision.audience import Audience
    from gc_testkit.helpers.tool_ctx import local_ctx
    from PIL import Image

    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)
    spy = install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": challenge.code}}]}
        ),
    )
    cfg = _cfg(launch_root=str(tmp_path))
    ctx = local_ctx(cfg)
    ctx.config_dict = cfg
    ctx.model_router = ModelRouter(cfg, level_provider=lambda: "default")
    ctx.image_store = ImageStore(str(tmp_path / "store"))
    ctx.on_trace_note = lambda text: None
    st = _state([])
    st.vision_probe_turn = "root"
    st.pending_tool_attachments = []
    st.vision_audience = Audience(None, None, True)
    source = tmp_path / "project.png"
    output = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(output, "PNG")
    source.write_bytes(output.getvalue())
    tool = ViewImage(ctx=ctx, state=st, config_dict=cfg)
    result = json.loads(await tool.acall(path=str(source)))
    assert result["attached"] and len(spy.requests) == 1
    st.vision_live_ids = frozenset(
        {image_id(normalize_image(output.getvalue()).data)}
    )
    result = json.loads(await tool.acall(path=str(source)))
    assert result["already_in_context"] and len(spy.requests) == 1


def test_root_and_children_keep_turn_identity_and_drop_transient_state():
    from types import SimpleNamespace

    from gc_core.persistence.state_serializer import StateSerializer
    from gc_core.turn_boundary import begin_turn
    from test_images_state_and_context import _state as real_state

    root = real_state()
    begin_turn(root, SimpleNamespace())
    first = root.vision_probe_turn
    assert first
    child = real_state()
    child.parent_state_id = root.state_id
    child.vision_probe_turn = first
    begin_turn(child, SimpleNamespace())
    assert child.vision_probe_turn == first
    root.vision_routes = {"secret": "route"}
    root.vision_probe_notices = {"notice"}
    payload = StateSerializer.serialize(root)
    assert not (
        {"vision_probe_turn", "vision_routes", "vision_probe_notices"}
        & payload.keys()
    )
    restored = StateSerializer.deserialize(payload)
    assert restored.vision_probe_turn is None and restored.vision_routes == {}
    begin_turn(root, SimpleNamespace())
    assert root.vision_probe_turn != first


async def test_transient_hides_repeat_tool_in_same_root(tmp_path, monkeypatch):
    from test_image_rejection_retry import VIEW_TOOL

    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)
    spy = install_transport(
        monkeypatch,
        lambda request: (
            httpx.Response(503)
            if pngs(request) == [challenge.png]
            else httpx.Response(200, json=OK)
        ),
    )
    ctx = _ctx(tmp_path, _cfg(), [])
    _, message = _user_with_image(ctx.image_store)
    st = _state([message], tools=[VIEW_TOOL])
    st.vision_probe_turn = "root"
    await pr._arequest_backend_canonical([message], st, ctx)
    await pr._arequest_backend_canonical([message], st, ctx)
    assert len(spy.requests) == 3
    assert not st.vision_audience.tool_visible
    assert all(
        not json.loads(r.content).get("tools") for r in spy.requests[1:]
    )


@pytest.mark.parametrize(
    "header", ["x_rq_id", "X_RQ_ID", "x-session-id", "X-Session-ID"]
)
@pytest.mark.parametrize("failure", [False, True])
async def test_explicit_header_replaces_automatic_value_and_reuses_probe(
    tmp_path, monkeypatch, header, failure
):
    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)
    spy = install_transport(
        monkeypatch,
        lambda request: (
            httpx.Response(503)
            if failure and pngs(request) == [challenge.png]
            else httpx.Response(
                200,
                json={"choices": [{"message": {"content": challenge.code}}]},
            )
        ),
    )
    ctx = _ctx(
        tmp_path, _cfg(chat_completion_headers={header: "static-route"}), []
    )
    _, message = _user_with_image(ctx.image_store)
    st = _state([message])
    st.vision_probe_turn = "root"
    await pr._arequest_backend_canonical([message], st, ctx)
    st.state_id = "another-thread"
    await pr._arequest_backend_canonical([message], st, ctx)
    assert sum(pngs(r) == [challenge.png] for r in spy.requests) == 1
    assert all(
        r.headers.get_list(header) == ["static-route"] for r in spy.requests
    )


async def test_changed_duplicate_routing_headers_require_new_probe(
    tmp_path, monkeypatch
):
    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)

    def handler(request):
        content = (
            challenge.code
            if request.headers.get_list("x-target")[-1] == "vision"
            else "wrong"
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    spy = install_transport(monkeypatch, handler)
    ctx = _ctx(
        tmp_path,
        _cfg(
            chat_completion_headers={"X-Target": "text", "x-target": "vision"}
        ),
        [],
    )
    _, message = _user_with_image(ctx.image_store)
    st = _state([message])
    st.vision_probe_turn = "root"
    await pr._arequest_backend_canonical([message], st, ctx)
    ctx.config_dict["chat_completion_headers"] = {
        "X-Target": "vision",
        "x-target": "text",
    }
    await pr._arequest_backend_canonical([message], st, ctx)
    assert sum(pngs(r) == [challenge.png] for r in spy.requests) == 3
    assert not pngs(spy.requests[-1])
