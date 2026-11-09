"""Проба модели из предохранителя (спека model-breaker §2.2): начало хода
главного трейса даёт открытым алиасам право на одну пробу, основной запрос
забирает его и идёт к модели одним HTTP-запросом."""

import asyncio
import json
import time
import types

import httpx
import pytest
from gc_core import processing_responses as pr
from gc_core import run_context as rc
from gc_core.config_modules.root import AppConfig
from gc_core.execution.contracts import (
    MainModelUnavailable,
    ModelRequestStopped,
)
from gc_core.model_router import breaker_reason
from gc_core.states import State
from gc_core.user_stop import UserStopInterrupt
from gc_testkit import as_coro, run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions", "use_retry": True},
    "model": "auto",
    "logging": {"save_logs": False},
}
USER = [{"role": "user", "content": "привет", "id": "m_u"}]


def _ctx(level: str = "default", **extra):
    raw = {**RAW, **extra}
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


class _Auth:
    def __init__(self):
        self.force_calls = 0

    def get_token(self, force=False):
        self.force_calls += int(force)
        return "tok"


def test_probe_survives_auth_retry(monkeypatch):
    ctx = _ctx()
    ctx.auth_connector = _Auth()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    sent = _transport(monkeypatch)
    pr.begin_turn_main_trace(s, ctx)
    first = pr.arequest_chat_completions_with_policy

    async def once_401(**kwargs):
        if not sent:
            sent.append(("401", kwargs))
            raise _http_error(401)
        return await first(**kwargs)

    monkeypatch.setattr(pr, "arequest_chat_completions_with_policy", once_401)
    run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert ctx.auth_connector.force_calls == 1
    assert [model for model, _ in sent] == ["401", "mid"]
    assert sent[0][1]["use_retry"] is False
    assert sent[1][1]["use_retry"] is False
    assert sent[1][1]["adaptive_retry"] is False


_STOPS = [
    asyncio.CancelledError,
    lambda: ModelRequestStopped("стоп"),
    lambda: UserStopInterrupt(kind="model"),
]


def _interrupt_first_send(monkeypatch, sent, stop):
    """Первая отправка обрывается стопом до ответа, дальше — штатно."""
    answer = pr.arequest_chat_completions_with_policy

    async def interrupted(**kwargs):
        if not sent:
            sent.append(("stop", kwargs))
            raise stop()
        return await answer(**kwargs)

    monkeypatch.setattr(
        pr, "arequest_chat_completions_with_policy", interrupted
    )


@pytest.mark.parametrize("stop", _STOPS)
def test_probe_right_returns_when_send_is_stopped(monkeypatch, stop):
    ctx = _ctx()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    sent = _transport(monkeypatch)
    pr.begin_turn_main_trace(s, ctx)
    _interrupt_first_send(monkeypatch, sent, stop)

    with pytest.raises(type(stop())):
        run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    breaker = ctx.model_router.breaker
    assert breaker.is_open("mid") is False
    assert breaker.known_unavailable("mid") == "ReadError"

    run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert sent[1][1]["use_retry"] is False
    assert sent[1][1]["adaptive_retry"] is False


def test_probe_right_returns_when_auth_retry_is_cancelled(monkeypatch):
    ctx = _ctx()
    ctx.auth_connector = _Auth()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    pr.begin_turn_main_trace(s, ctx)
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _http_error(401)
        raise asyncio.CancelledError

    monkeypatch.setattr(pr, "arequest_chat_completions_with_policy", fake)
    with pytest.raises(asyncio.CancelledError):
        run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert [kw["use_retry"] for kw in calls] == [False, False]
    assert ctx.model_router.breaker.known_unavailable("mid") == "ReadError"


def test_probe_answered_with_error_spends_right(monkeypatch):
    ctx = _ctx()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    pr.begin_turn_main_trace(s, ctx)

    async def bad_request(**_kwargs):
        raise _http_error(400)

    monkeypatch.setattr(
        pr, "arequest_chat_completions_with_policy", bad_request
    )
    with pytest.raises(httpx.HTTPStatusError):
        run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert ctx.model_router.breaker.known_unavailable("mid") is None


def test_probe_right_kept_when_preparation_fails(monkeypatch):
    ctx = _ctx()
    s = _state(ctx)
    ctx.model_router.breaker.trip("mid", "ReadError")
    sent = _transport(monkeypatch)
    pr.begin_turn_main_trace(s, ctx)

    async def broken_prep(*_args, **_kwargs):
        raise ValueError("prep")

    monkeypatch.setattr(pr, "_aprepare_native_vision", broken_prep)
    with pytest.raises(ValueError):
        run_coro(pr._arequest_backend_canonical(USER, s, ctx))
    assert sent == []
    assert ctx.model_router.breaker.take_probe("mid") is True


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


def test_durable_turn_start_clears_open_alias_without_probe():
    """Temporal: ход ждёт модель по политике повторов activity, пробы нет —
    начало хода снимает отметку, и activity получает пустой снимок."""
    ctx = _ctx()
    s = _state(ctx)
    ctx.durable_execution = True
    ctx.execution_patch = lambda patch: True
    ctx.operations = types.SimpleNamespace(monotonic=time.monotonic)
    breaker = ctx.model_router.breaker
    breaker.trip("mid", "ReadError")
    pr.begin_turn_main_trace(s, ctx)
    assert breaker.snapshot() == {}
    assert breaker.known_unavailable("mid") is None
    assert breaker.take_probe("mid") is False


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


def _aux_transport(monkeypatch):
    from gc_core.utils import llm_chat_client as lcc

    sent = []

    async def fake(**kwargs):
        sent.append(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}, {}

    monkeypatch.setattr(lcc, "_arequest_admitted_chat_completion", fake)
    return sent


def _aux_call(ctx, **extra):
    from gc_core.utils.llm_chat_client import arequest_chat_completion_text

    return run_coro(
        arequest_chat_completion_text(
            ctx=ctx,
            state=_state(ctx),
            messages=[{"role": "user", "content": "x"}],
            max_tokens=64,
            consumer="summary",
            **extra,
        )
    )


def _open_with_right(ctx, alias="mid"):
    ctx.model_router.breaker.trip(alias, "ReadError")
    ctx.model_router.breaker.allow_probe()


def test_aux_call_takes_probe_once(monkeypatch):
    ctx = _ctx()
    sent = _aux_transport(monkeypatch)
    _open_with_right(ctx)
    _aux_call(ctx)
    assert sent[0]["use_retry"] is False
    assert sent[0]["adaptive_retry"] is False
    _aux_call(ctx)
    assert sent[1]["use_retry"] is True
    assert sent[1]["adaptive_retry"] is True


@pytest.mark.parametrize("stop", _STOPS)
def test_aux_probe_right_returns_when_call_is_stopped(monkeypatch, stop):
    from gc_core.utils import llm_chat_client as lcc

    ctx = _ctx()
    _open_with_right(ctx)

    async def interrupted(**_kwargs):
        raise stop()

    monkeypatch.setattr(lcc, "_arequest_admitted_chat_completion", interrupted)
    with pytest.raises(type(stop())):
        _aux_call(ctx)
    assert ctx.model_router.breaker.known_unavailable("mid") == "ReadError"

    sent = _aux_transport(monkeypatch)
    _aux_call(ctx)
    assert sent[0]["use_retry"] is False
    assert ctx.model_router.breaker.known_unavailable("mid") is None


def test_isolated_call_keeps_probe_right(monkeypatch):
    ctx = _ctx("max", assign={"summary": "mid"})
    sent = _aux_transport(monkeypatch)
    _open_with_right(ctx)
    _aux_call(ctx, isolated_timeout_sec=5.0)
    models = [kw["payload_factory"](64)["model"] for kw in sent]
    assert models == ["big"]
    assert ctx.model_router.breaker.take_probe("mid") is True


def test_snapshot_does_not_take_probe(monkeypatch):
    ctx = _ctx()
    sent = _aux_transport(monkeypatch)
    _open_with_right(ctx)

    async def before_send(_prepared):
        return None

    _aux_call(ctx, before_send=before_send)
    assert sent[0]["use_retry"] is False
    assert sent[0]["adaptive_retry"] is False
