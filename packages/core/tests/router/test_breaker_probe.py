"""Проба модели из предохранителя (спека model-breaker §2.2): начало хода
главного трейса даёт открытым алиасам право на одну пробу, основной запрос
забирает его и идёт к модели одним HTTP-запросом."""

import json

import httpx
import pytest
from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.config_modules.root import AppConfig
from gc_core.execution.contracts import MainModelUnavailable
from gc_core.model_router import breaker_reason
from gc_core.states import State
from gc_testkit import as_coro, run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions", "use_retry": True},
    "model": "auto",
    "logging": {"save_logs": False},
}
USER = [{"role": "user", "content": "привет", "id": "m_u"}]


def _ctx(level: str = "default"):
    raw = dict(RAW)
    raw["agent"] = {
        "auto_routing_safe_level": "default",
        "auto_routing_two_level": {"weak_steps": 10, "strong_steps": 2},
    }
    cfg = AppConfig.model_validate(raw)
    return run_coro(rc.acreate_run_context(cfg, choice_provider=lambda: level))


def _state(ctx) -> State:
    s = State(
        verbose=False,
        model="m",
        tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description,
        skills_list=[],
        max_steps=50,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    s.backend_api_mode = "chat_completions"
    s.messages = [{"role": "system", "content": "SYS", "id": "m_sys"}]
    ctx.attach_state(s)
    return s


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://backend.local/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


def _read_reply(n: int) -> dict:
    call = {
        "id": f"c{n}",
        "type": "function",
        "function": {
            "name": "fs_read_file",
            "arguments": json.dumps({"path": f"src/c{n}.py"}),
        },
    }
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    return {"choices": [{"message": message, "finish_reason": "tool_calls"}]}


def _transport(monkeypatch, dead=()):
    """Подмена политики запроса: модели из `dead` отвечают 503, остальные —
    одним чтением. Журнал — (модель, kwargs) каждого вызова; id модели в
    реестре RAW совпадает с алиасом."""
    sent = []

    def fake(**kwargs):
        payload = kwargs["payload_factory"](kwargs["initial_max_tokens"])
        sent.append((payload["model"], kwargs))
        if payload["model"] in dead:
            raise _http_error(503)
        return _read_reply(len(sent)), payload

    monkeypatch.setattr(
        pr, "arequest_chat_completions_with_policy", as_coro(fake)
    )
    return sent


def test_turn_start_gives_probe_right_to_open_alias():
    ctx = _ctx()
    ctx.model_router.breaker.trip("mid", "ReadError")
    pr.begin_turn_main_trace(_state(ctx), ctx)
    assert ctx.model_router.breaker.is_open("mid") is False
    assert ctx.model_router.breaker.known_unavailable("mid") == "ReadError"


def test_probe_call_is_single_request_without_retries(monkeypatch):
    ctx = _ctx()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    sent = _transport(monkeypatch)
    pr.begin_turn_main_trace(s, ctx)

    run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert len(sent) == 1
    assert sent[0][1]["use_retry"] is False
    assert sent[0][1]["adaptive_retry"] is False

    run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert len(sent) == 2
    assert sent[1][1]["use_retry"] is True
    assert sent[1][1]["adaptive_retry"] is True


def test_failed_probe_reopens_and_blocks_rest_of_turn(monkeypatch):
    ctx = _ctx()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    sent = _transport(monkeypatch, dead={"mid"})
    pr.begin_turn_main_trace(s, ctx)

    with pytest.raises(MainModelUnavailable):
        run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert len(sent) == 1
    assert ctx.model_router.breaker.is_open("mid") is True

    with pytest.raises(MainModelUnavailable):
        pr.resolve_main_for_call(s, ctx)
    assert len(sent) == 1
    reason = ctx.model_router.breaker.snapshot()["mid"]
    assert reason == breaker_reason(_http_error(503))
    assert "503" in reason
    assert "в предохранителе" not in reason


def test_next_turn_probes_again(monkeypatch):
    ctx = _ctx()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    _transport(monkeypatch, dead={"mid"})
    pr.begin_turn_main_trace(s, ctx)
    with pytest.raises(MainModelUnavailable):
        run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert ctx.model_router.breaker.is_open("mid") is True

    pr.begin_turn_main_trace(s, ctx)
    assert ctx.model_router.breaker.is_open("mid") is False


def test_child_pickup_gives_no_probe_right():
    ctx = _ctx()
    s = _state(ctx)
    s.parent_state_id = "p"
    ctx.model_router.breaker.trip("mid", "ReadError")
    with pytest.raises(RuntimeError):
        pr.begin_turn_main_trace(s, ctx)
    assert ctx.model_router.breaker.is_open("mid") is True


def test_without_patch_marker_turn_start_fails_as_before():
    ctx = _ctx()
    ctx.execution_patch = lambda patch: patch != pr.MODEL_BREAKER_PROBE_PATCH
    ctx.model_router.breaker.trip("mid", "ReadError")
    with pytest.raises(MainModelUnavailable):
        pr.begin_turn_main_trace(_state(ctx), ctx)
    assert ctx.model_router.breaker.is_open("mid") is True


def test_auto_failed_probe_reroutes_in_same_tick(monkeypatch):
    ctx = _ctx("auto")
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    sent = _transport(monkeypatch, dead={"mid"})
    pr.begin_turn_main_trace(s, ctx)

    run_coro(pr.aprocess_user_ask([], s, ctx))

    models = [model for model, _ in sent]
    assert models == ["mid", "big"]
    probe_kwargs = sent[0][1]
    assert probe_kwargs["use_retry"] is False
    assert probe_kwargs["adaptive_retry"] is False
    assert s._call_level == "max"
    assert ctx.model_router.breaker.is_open("mid") is True
