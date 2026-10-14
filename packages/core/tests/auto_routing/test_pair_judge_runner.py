"""Вызов судьи пары: потребитель, метка, промпт, выбор пары, событие, запасной выбор."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from gc_core.auto_routing import judge_runner, state_ops
from gc_core.auto_routing.pair_judge import DEFAULT_PROMPT_PATH
from gc_core.auto_routing.policy import RoutingParams
from gc_core.intent_log import append_user_ask
from gc_core.model_router import ModelRouter
from gc_core.usage.account import CATEGORY_ROUTING_JUDGE, UsageAccount, category_for_request_label
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx, make_settings, update_ctx

REGISTRY = {"models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"}}
PARAMS = RoutingParams(safe_level="auto", pair_threshold=4)


def _state():
    state = SimpleNamespace()
    state_ops.reset(state)
    state.messages = [{"role": "system", "content": "SYS"}]
    state.current_user_intent = append_user_ask([], "add a caching layer to the api", user_turn_seq=0)
    state.user_intent_log = []
    state.user_turn_seq = 0
    state.turn_messages_start = None
    state.usage = UsageAccount()
    return state


def _ctx(events):
    logger = SimpleNamespace(info=lambda msg, *a, **k: events.append(k.get("extra", {})),
                             warning=lambda *a, **k: None)
    loaded = make_settings({"backend.api_mode": "chat_completions", **REGISTRY})
    return fake_ctx(
        loaded=loaded,
        model_router=ModelRouter(loaded, level_provider=lambda: "default"),
        logger=logger,
        auxiliary_llm=SimpleNamespace(completion_kwargs={}, judge_max_tokens=256, max_tokens=1024),
        routing_params=PARAMS, is_tui=False,
    )


def _stub_backend(monkeypatch, reply, seen=None):
    async def fake(*, ctx, state, messages, max_tokens, request_label, consumer, payload_overrides=None):
        if seen is not None:
            seen.append({"consumer": consumer, "label": request_label,
                         "system": messages[0]["content"], "user": messages[1]["content"]})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(judge_runner, "arequest_auxiliary_completion_text", fake)


def _run(events, mid_turn=False):
    return run_coro(judge_runner.arun_pair_judge(_state(), _ctx(events), params=PARAMS, mid_turn=mid_turn))


@pytest.mark.parametrize("reply, level", [('{"score": 4, "reason": "multi-module"}', "max"),
                                          ('{"score": 3, "reason": "one module"}', "default")])
def test_the_score_picks_the_pair_through_the_routing_judge_binding(monkeypatch, reply, level):
    seen, events = [], []
    _stub_backend(monkeypatch, reply, seen)
    choice = _run(events)
    assert choice.safe_level == level and choice.source == "judge" and choice.error is None
    assert seen[0]["consumer"] == "routing_judge" and seen[0]["label"] == "routing_judge_pair"
    assert '"request": "add a caching layer to the api"' in seen[0]["user"]
    assert "score" in seen[0]["system"]


def test_the_event_carries_the_score_the_pair_and_the_source(monkeypatch):
    events = []
    _stub_backend(monkeypatch, '{"score": 2, "reason": "one file"}')
    _run(events, mid_turn=True)
    (ev,) = [e for e in events if e.get("event") == "routing_pair_judge"]
    assert ev["turn"] == 1 and ev["mid_turn"] is True
    assert ev["score"] == 2 and ev["reason"] == "one file" and ev["pair_threshold"] == 4
    assert ev["safe_level"] == "default" and ev["pair"] == ["light", "default"]
    assert ev["source"] == "judge" and "error" not in ev
    assert ev["card_chars"] > 0 and ev["latency_ms"] >= 0


@pytest.mark.parametrize("reply, error", [("no json here", "bad_json"),
                                          ('{"score": 9}', "out_of_range"),
                                          (RuntimeError("backend down"), "RuntimeError: backend down")])
def test_a_failure_is_the_hard_pair_and_never_an_exception(monkeypatch, reply, error):
    events = []
    _stub_backend(monkeypatch, reply)
    choice = _run(events)
    assert choice.safe_level == "max" and choice.source == "fallback" and choice.score is None
    assert choice.error == error
    (ev,) = [e for e in events if e.get("event") == "routing_pair_judge"]
    assert ev["source"] == "fallback" and ev["error"] == error and ev["pair"] == ["default", "max"]


def test_the_usage_goes_to_the_routing_judge_category():
    assert category_for_request_label(judge_runner.PAIR_REQUEST_LABEL) == CATEGORY_ROUTING_JUDGE


def test_the_pair_prompt_is_a_bundled_asset_with_the_scale_and_the_format():
    text = Path(DEFAULT_PROMPT_PATH).read_text(encoding="utf-8")
    for marker in ('"score"', '"reason"', "1 —", "5 —", "higher"):
        assert marker in text, marker


def test_durable_judge_uses_resource_and_clock_ports_without_local_reads(monkeypatch):
    seen, events, resources = [], [], []
    _stub_backend(monkeypatch, '{"score": 3, "reason": "routine"}', seen)
    ctx = _ctx(events)
    ctx.durable_execution = True
    ticks = iter((1.0, 1.25))
    ctx.operations = SimpleNamespace(monotonic=lambda: next(ticks))

    async def resource(request):
        resources.append(request)
        return "Worker pair prompt."

    def forbidden(*args, **kwargs):
        raise AssertionError("local resource or clock read inside workflow")

    ctx.prompt_resource = resource
    monkeypatch.setattr(judge_runner, "_load_judge_prompt", forbidden)
    monkeypatch.setattr(judge_runner, "time", SimpleNamespace(monotonic=forbidden))
    choice = run_coro(judge_runner.arun_pair_judge(_state(), ctx, params=PARAMS, mid_turn=False))
    assert choice.source == "judge"
    assert resources[0].kind == "routing_pair_judge"
    assert seen[0]["system"] == "Worker pair prompt."
    event = next(e for e in events if e.get("event") == "routing_pair_judge")
    assert event["latency_ms"] == 250


@pytest.mark.parametrize("missing", ["operations", "prompt_resource"])
def test_missing_durable_ports_propagate_failure(monkeypatch, missing):
    from gc_core.execution.contracts import ExecutionFailure

    ctx = _ctx([])
    ctx.durable_execution = True
    ctx.operations = SimpleNamespace(monotonic=lambda: 1.0)

    async def resource(request):
        return "Worker pair prompt."

    ctx.prompt_resource = resource
    delattr(ctx, missing)
    with pytest.raises(ExecutionFailure, match=missing):
        run_coro(judge_runner.arun_pair_judge(_state(), ctx, params=PARAMS, mid_turn=False))


def test_worker_resource_loader_reads_the_configured_pair_prompt(tmp_path):
    from gc_core.execution.resources import PromptResourceRequest
    from gc_core.resource_loading import load_prompt_resource

    own = tmp_path / "pair.md"
    own.write_text("Configured worker pair prompt.", encoding="utf-8")
    ctx = fake_ctx({"paths.chat_completions_routing_pair_judge_prompt_path": str(own)})
    assert load_prompt_resource(PromptResourceRequest("routing_pair_judge"), ctx=ctx) == "Configured worker pair prompt."


@pytest.mark.parametrize("content", [None, "  \n"])
def test_a_missing_or_empty_prompt_is_the_hard_pair_with_a_warning(monkeypatch, tmp_path, content):
    """Сломанный путь к инструкции судьи — сбой судьи: верхняя пара и видимое
    предупреждение с путём (спека PR140 Р7), а не отказ запуска."""
    from gc_core import runtime_notices

    prompt = tmp_path / "pair.md"
    if content is not None:
        prompt.write_text(content, encoding="utf-8")
    pushed, warned = [], []
    monkeypatch.setattr(runtime_notices, "push_runtime_notice", pushed.append)
    _stub_backend(monkeypatch, '{"score": 2, "reason": "x"}')
    ctx = _ctx([])
    ctx.is_tui = True
    ctx.logger = SimpleNamespace(info=lambda *a, **k: None,
                                 warning=lambda msg, *a, **k: warned.append(msg))
    update_ctx(ctx, {"paths.chat_completions_routing_pair_judge_prompt_path": str(prompt)})
    choice = run_coro(judge_runner.arun_pair_judge(_state(), ctx, params=PARAMS, mid_turn=False))
    assert choice.safe_level == "max" and choice.source == "fallback"
    assert any(str(prompt) in n.message for n in pushed)
    assert any(str(prompt) in w for w in warned)
