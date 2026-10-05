import json

import httpx
import pytest
from gc_core.model_router import ModelRouter
from gc_core.vision import probe
from gc_core.vision.eyes import adescribe_image
from gc_core.vision.probe import make_challenge
from gc_core.vision.service import VisionNotConfirmed
from test_image_rejection_retry import OK, _cfg, _ctx, _state, _user_with_image
from test_vision_probe_flow import pngs
from gc_testkit.settings import make_settings
from transport_fakes import install_transport


def eyes_config(
    mode="responses", *, tokens=True, eyes_target="eyes", main_backend=None
):
    """Две модели реестра: основная и «глаза» со своим доступом.

    ``tokens=False`` — без своего адреса и токена: запись работает через общее
    подключение и наследует авторизацию ``auth_connector`` (свой адрес без
    токена схема не пропускает)."""

    def access(host, token):
        return {"url": f"http://{host}/chat", "token": token} if tokens else {}

    main = {"model": "main-model", **access("main.test", "main-token")}
    if main_backend is not None:
        main["backend"] = main_backend
    return _cfg(
        **{"backend.api_mode": mode},
        models={
            "main": main,
            "eyes": {
                "model": "eyes-model",
                **access("eyes.test", "eyes-token"),
                "backend": {
                    "chat_completion_headers": {"x-target": eyes_target}
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
    assert len(spy.requests) == 4
    assert [r.url.host for r in spy.requests] == [
        "eyes.test",
        "eyes.test",
        "main.test",
        "main.test",
    ]
    assert all(pngs(r) == [challenge.png] for r in spy.requests[2:])
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
    cfg = eyes_config(
        mode, tokens=False, eyes_target="old", main_backend={"vision": False}
    )

    class Auth:
        token = "old-token"
        calls = 0

        async def aget_token(self, force=False):
            self.calls += 1
            return self.token

    auth = Auth()

    def handler(request):
        # Без своих адресов обе модели идут на общий адрес — «глаза» узнаются
        # по id модели в теле запроса.
        if json.loads(request.content).get("model") == "eyes-model":
            access = (
                request.headers["Authorization"]
                if change == "token"
                else request.headers["x-target"]
            )
            if access in {"Bearer old-token", "old"}:
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
        cfg = eyes_config(
            mode,
            tokens=False,
            eyes_target="new",
            main_backend={"vision": False},
        )
        ctx.model_router = ModelRouter(
            make_settings(cfg), level_provider=lambda: "default"
        )
    begin_turn(st, ctx)
    await pr._arequest_backend_canonical([message], st, ctx)
    assert st.vision_audience.tool_visible
    assert b"fs_view_image" in spy.requests[-1].content
    assert len(spy.requests) == 2  # Probe happens only when the tool is used.
    assert (
        auth.calls == 2
    )  # Main and candidate key share one auth acquisition.
    audience = await aensure_view_audience(ctx, st)
    assert audience.eyes is not None and audience.tool_visible
    assert len(spy.requests) == 3
