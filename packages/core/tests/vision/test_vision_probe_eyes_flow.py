import httpx
import pytest
from gc_core.model_router import ModelRouter
from gc_core.vision import probe
from gc_core.vision.eyes import adescribe_image
from gc_core.vision.probe import make_challenge
from gc_core.vision.service import VisionNotConfirmed
from test_image_rejection_retry import OK, _cfg, _ctx, _state, _user_with_image
from test_vision_probe_flow import pngs
from transport_fakes import install_transport


def eyes_config(mode="responses"):
    return _cfg(
        backend_api_mode=mode,
        models={
            "main": {
                "model": "main-model",
                "url": "http://main.test/chat",
                "authorization_header": "Bearer main-token",
            },
            "eyes": {
                "model": "eyes-model",
                "url": "http://eyes.test/chat",
                "authorization_header": "Bearer eyes-token",
                "backend_overrides": {
                    "chat_completion_headers": {"x-target": "eyes"}
                },
            },
        },
        slots={"default": "main"},
        assign={"vision": "eyes"},
    )


async def test_assigned_eyes_in_responses_use_own_access_and_probe_once(
    tmp_path, monkeypatch
):
    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)

    def handler(request):
        answer = (
            challenge.code
            if pngs(request) == [challenge.png]
            else "blue square"
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": answer}, "finish_reason": "stop"}
                ]
            },
        )

    spy = install_transport(monkeypatch, handler)
    ctx = _ctx(tmp_path, eyes_config(), [])
    att, message = _user_with_image(ctx.image_store)
    st = _state([message])
    st.vision_probe_turn = "root"
    for _ in range(2):
        assert (
            await adescribe_image(
                ctx, st, att, question="what", task_context="private task"
            )
            == "blue square"
        )
    assert len(spy.requests) == 3
    assert all(
        r.headers["Authorization"] == "Bearer eyes-token"
        and r.headers["x-target"] == "eyes"
        for r in spy.requests
    )
    assert b"private task" not in spy.requests[0].content
    assert all(b"private task" in r.content for r in spy.requests[1:])


async def test_fallback_recipient_must_pass_its_own_probe(
    tmp_path, monkeypatch
):
    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)

    def handler(request):
        if request.url.host == "eyes.test":
            if pngs(request) == [challenge.png]:
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": challenge.code}}]
                    },
                )
            return httpx.Response(401)
        return httpx.Response(200, json=OK)

    spy = install_transport(monkeypatch, handler)
    ctx = _ctx(tmp_path, eyes_config(), [])
    att, message = _user_with_image(ctx.image_store)
    st = _state([message])
    st.vision_probe_turn = "root"
    with pytest.raises(VisionNotConfirmed):
        await adescribe_image(
            ctx, st, att, question="what", task_context="private task"
        )
    assert len(spy.requests) == 5
    assert [r.url.host for r in spy.requests] == [
        "eyes.test",
        "eyes.test",
        "eyes.test",
        "main.test",
        "main.test",
    ]
    assert all(pngs(r) == [challenge.png] for r in spy.requests[3:])
    assert not ctx.model_router.breaker.is_open("main")


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
@pytest.mark.parametrize("change", ["token", "routing"])
async def test_changed_eyes_access_restores_tool_without_probing_text_request(
    tmp_path, monkeypatch, mode, change
):
    from gc_core import processing_responses as pr
    from gc_core.turn_boundary import begin_turn
    from gc_core.vision.service import aensure_view_audience
    from test_image_rejection_retry import VIEW_TOOL

    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)
    cfg = eyes_config(mode)
    cfg["agent_url"] = "http://main.test/responses"
    for model in cfg["models"].values():
        model.pop("authorization_header")
    cfg["models"]["main"]["backend_overrides"] = {"vision": False}
    cfg["models"]["eyes"]["backend_overrides"]["chat_completion_headers"] = {
        "x-target": "old"
    }

    class Auth:
        token = "old-token"
        calls = 0

        async def aget_token(self, force=False):
            self.calls += 1
            return self.token

    auth = Auth()

    def handler(request):
        if request.url.host == "eyes.test":
            access = (
                request.headers["Authorization"]
                if change == "token"
                else request.headers["x-target"]
            )
            if access in {"Bearer old-token", "old-token", "old"}:
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": challenge.code}}]},
            )
        if mode == "responses":
            return httpx.Response(
                200,
                text='data: {"type":"response.completed","response":{"id":"r1","output":[]}}\n\n',
            )
        return httpx.Response(200, json=OK)

    spy = install_transport(monkeypatch, handler)
    ctx = _ctx(tmp_path, cfg, [])
    ctx.auth_connector = auth
    _, message = _user_with_image(ctx.image_store)
    st = _state([message], tools=[VIEW_TOOL])
    st.call_metadata = {}
    st.previous_response_id = None
    st.vision_probe_turn = "root-a"
    assert not (await aensure_view_audience(ctx, st)).tool_visible
    assert auth.calls == 1
    if change == "token":
        auth.token = "new-token"
    else:
        cfg["models"]["eyes"]["backend_overrides"][
            "chat_completion_headers"
        ] = {"x-target": "new"}
        ctx.model_router = ModelRouter(cfg, level_provider=lambda: "default")
    begin_turn(st, ctx)
    await pr._arequest_backend_canonical([message], st, ctx)
    assert st.vision_audience.tool_visible
    assert b"fs_view_image" in spy.requests[-1].content
    assert len(spy.requests) == 3  # Two rejected auth forms, then the text request.
    assert (
        auth.calls == 2
    )  # Main and candidate key share one auth acquisition.
    audience = await aensure_view_audience(ctx, st)
    assert audience.eyes is not None and audience.tool_visible
    assert len(spy.requests) == 4
