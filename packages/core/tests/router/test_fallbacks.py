"""Task 9 (план «Профили v2, волна A»): фоллбэки недоступности (спека §8).

Per-alias предохранитель (`model_router.CircuitBreaker`), деградация служебных
потребителей на активную модель, рестарт/деградация субагента с пином, честная
ошибка класса 2 (main-трейс). См.
.superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-9-brief.md.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter
from gc_core.runtime_notices import (
    clear_runtime_notices,
    collect_runtime_notices,
)
from gc_core.states import SharedStateData, State
from gc_core.utils import llm_chat_client
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx


def _snapshot(raw: dict) -> AppConfig:
    return AppConfig.model_validate(raw)


def _router(raw: dict, *, level: str = "default") -> ModelRouter:
    return ModelRouter(_snapshot(raw), level_provider=lambda: level)


def _http_error(status: int) -> httpx.HTTPStatusError:
    """HTTP-отказ бэкенда в форме ДЕЙСТВУЮЩЕГО транспорта.

    Тест приехал с develop, где путь к модели шёл через `requests`. Здесь он
    httpx, и `is_model_unavailable_error` спрашивает про `httpx.HTTPStatusError`
    — на requests-исключении предикат просто не срабатывал бы, и тест зеленел бы,
    ничего не проверив.
    """
    request = httpx.Request("POST", "http://backend.local/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        "http error", request=request, response=response
    )


def _service_state(**overrides) -> SimpleNamespace:
    base = dict(
        state_id="state-1",
        x_target=None,
        default_model="legacy-model",
        usage=None,
        messages=[],
        tools=[],
        call_metadata={},
        previous_response_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _service_ctx(router: ModelRouter, *, is_tui: bool = False):
    return fake_ctx(
        {
            "backend.completions_url": "http://backend.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
            "backend.api_mode": "chat_completions",
        },
        model_router=router,
        logger=None,
        auth_connector=None,
        is_tui=is_tui,
    )


@pytest.fixture(autouse=True)
def _clean_notices():
    clear_runtime_notices()
    yield
    clear_runtime_notices()


# ---------------------------------------------------------------------------
# Класс 1 — служебные потребители (llm_chat_client.request_chat_completion_text)
# ---------------------------------------------------------------------------


def test_service_consumer_trips_breaker_after_pinned_model_fails(monkeypatch):
    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"finish_gate": "flash"},
        },
        level="default",
    )
    ctx = _service_ctx(router)
    calls = {"n": 0}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(500)
        payload = payload_factory(initial_max_tokens)
        return {
            "choices": [{"message": {"content": "active answer"}}]
        }, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    out = run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_service_state(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="finish_gate",
        )
    )

    assert out == "active answer"
    assert router.breaker.is_open("flash") is True
    assert router.breaker.is_open("glm-local") is False
    assert calls["n"] == 2


def test_service_consumer_open_alias_skips_wasted_transport_call(monkeypatch):
    """Открытый алиас — мгновенный отказ резолва, без похода в транспорт: счётчик
    вызовов транспорта на мёртвый алиас == 0 (только один вызов активной моделью)."""
    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"finish_gate": "flash"},
        },
        level="default",
    )
    router.breaker.trip("flash", "5xx после ретраев")
    ctx = _service_ctx(router)
    calls = {"n": 0}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        calls["n"] += 1
        payload = payload_factory(initial_max_tokens)
        return {
            "choices": [{"message": {"content": "active answer"}}]
        }, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    out = run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_service_state(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="finish_gate",
        )
    )

    assert out == "active answer"
    assert (
        calls["n"] == 1
    )  # ровно один вызов — активной моделью, пин не трогали


def test_service_consumer_degrades_and_pushes_fallback_notice(monkeypatch):
    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"finish_gate": "flash"},
        },
        level="default",
    )
    ctx = _service_ctx(router, is_tui=True)
    calls = {"n": 0}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(503)
        payload = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "ok"}}]}, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_service_state(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="finish_gate",
        )
    )

    notices = collect_runtime_notices()
    assert len(notices) == 1
    message = notices[0].message
    assert "finish_gate" in message
    assert "flash" in message
    assert "glm-local" in message


def test_double_failure_service_consumer_propagates_for_per_consumer_fail_semantics(
    monkeypatch,
):
    """Двойной отказ (пин и активная тоже недоступна) — исключение уходит наверх
    без изменений; per-consumer fail-семантика (finish_gate/summary/...) решает сама."""
    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"finish_gate": "flash"},
        },
        level="default",
    )
    ctx = _service_ctx(router)

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        raise _http_error(500)

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(
            llm_chat_client.arequest_chat_completion_text(
                ctx=ctx,
                state=_service_state(),
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=10,
                consumer="finish_gate",
            )
        )

    assert router.breaker.is_open("flash") is True
    assert router.breaker.is_open("glm-local") is True


def test_no_binding_no_degradation_attempt_when_already_active(monkeypatch):
    """Без привязки consumer уже резолвится в активную модель — деградировать
    некуда; исключение уходит наверх без второй (бессмысленной) попытки."""
    router = _router(
        {"models": {"glm-local": {}}, "slots": {"default": "glm-local"}},
        level="default",
    )
    ctx = _service_ctx(router)
    calls = {"n": 0}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        calls["n"] += 1
        raise _http_error(500)

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(
            llm_chat_client.arequest_chat_completion_text(
                ctx=ctx,
                state=_service_state(),
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=10,
                consumer="summary",
            )
        )

    assert calls["n"] == 1  # ни одной лишней (идентичной) попытки


def _judge_text(ctx):
    return run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_service_state(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="command_judge",
            isolated_timeout_sec=60,
        )
    )


def test_isolated_call_failure_leaves_the_main_model_alive(monkeypatch):
    """Судья без привязки идёт моделью основного хода. Его 5xx — забота самого
    судьи: алиас основного не попадает в предохранитель, следующий ход
    резолвится как обычно; повторов клиента нет, таймаут — свой."""
    router = _router(
        {"models": {"glm-local": {}}, "slots": {"default": "glm-local"}},
        level="default",
    )
    ctx = _service_ctx(router)
    seen = []

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        seen.append(kwargs)
        raise _http_error(500)

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    with pytest.raises(httpx.HTTPStatusError):
        _judge_text(ctx)

    assert router.breaker.is_open("glm-local") is False
    assert router.resolve("main").alias == "glm-local"
    assert len(seen) == 1
    assert seen[0]["use_retry"] is False
    assert seen[0]["adaptive_retry"] is False
    assert seen[0]["timeout"] == 60


def test_isolated_call_does_not_degrade_on_a_fresh_failure(monkeypatch):
    """Привязанный судья упал: ни отметки в предохранителе, ни второго
    запроса активной моделью — повторяет шаг судьи, а не клиент."""
    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"command_judge": "flash"},
        },
        level="default",
    )
    ctx = _service_ctx(router)
    calls = {"n": 0}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        calls["n"] += 1
        raise _http_error(500)

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    with pytest.raises(httpx.HTTPStatusError):
        _judge_text(ctx)

    assert calls["n"] == 1
    assert router.breaker.is_open("flash") is False
    assert router.breaker.is_open("glm-local") is False


def test_isolated_call_still_degrades_when_the_alias_is_already_open(
    monkeypatch,
):
    """Деградация на активную модель при уже открытом предохранителе — как
    раньше: мёртвый алиас не спрашивается, отвечает активная."""
    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"command_judge": "flash"},
        },
        level="default",
    )
    router.breaker.trip("flash", "down earlier")
    ctx = _service_ctx(router)
    calls = {"n": 0}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        calls["n"] += 1
        payload = payload_factory(initial_max_tokens)
        return {
            "choices": [{"message": {"content": "active answer"}}]
        }, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    assert _judge_text(ctx) == "active answer"
    assert calls["n"] == 1
    assert router.breaker.is_open("glm-local") is False


# ---------------------------------------------------------------------------
# finish_gate — двойной отказ → его fail-open семантика; command_judge fail-closed
# ---------------------------------------------------------------------------


def test_finish_gate_guard_double_failure_is_fail_open_error(monkeypatch):
    from gc_core.intent_guard.guard import FinishGateGuard

    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"finish_gate": "flash"},
        },
        level="default",
    )
    ctx = fake_ctx(
        {
            "backend.completions_url": "http://backend.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": True,
        },
        model_router=router,
        logger=None,
        auth_connector=None,
        is_tui=False,
    )
    state = State(
        verbose=False,
        model="glm-local",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        raise _http_error(500)

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    decision = run_coro(
        FinishGateGuard(ctx=ctx, state=state).aevaluate_turn_finish(
            finish_handoff_arguments="done"
        )
    )

    assert decision.outcome == "fail_open_error"


async def test_command_judge_fail_closed_semantics_unchanged(monkeypatch):
    from gc_core.security import command_moderation

    router = _router(
        {
            "models": {"flash": {}, "glm-local": {}},
            "slots": {"default": "glm-local"},
            "assign": {"command_judge": "flash"},
        },
        level="default",
    )
    ctx = fake_ctx(
        {
            "backend.completions_url": "http://backend.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
            "backend.api_mode": "chat_completions",
        },
        model_router=router,
        logger=None,
        auth_connector=None,
        is_tui=False,
        auxiliary_llm=SimpleNamespace(
            max_tokens=500, judge_max_tokens=200, completion_kwargs={}
        ),
    )

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        raise _http_error(500)

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    # Судья КОРУТИННЫЙ: его ждут на loop вызывающего. Здесь стоял синхронный
    # `run_command_judge_with_retry` — он получал корутину, отдавал её в
    # `validate_judge_result`, и тест зеленел на «это не словарь» вместо
    # «бэкенд ответил 500». Сама корутина оставалась неждаемой.
    judge_client = command_moderation._default_judge_client(
        (), {"ctx": ctx, "state": _service_state()}
    )
    with pytest.raises(Exception) as judge_failure:
        await judge_client({"tool_name": "execute_command"})
    assert (
        "500" in str(judge_failure.value)
        or "http" in str(judge_failure.value).lower()
    ), f"судья упал не по той причине: {judge_failure.value!r}"

    # command_judge остаётся fail-closed: судья недоступен → deny-mode блокирует.
    decision = command_moderation.decide_command_execution(
        "deny", None, judge_available=False, user_confirmation=None
    )
    assert decision.allowed is False
    assert decision.reason_code == "judge_unavailable"


# ---------------------------------------------------------------------------
# Класс 2 (main-трейс) — честная ошибка + предохранитель
# ---------------------------------------------------------------------------


def test_main_trace_trips_breaker_and_raises_honest_error_with_hint(
    monkeypatch,
):
    from gc_core import processing_responses as pr

    router = _router(
        {"models": {"glm-local": {}}, "slots": {"default": "glm-local"}},
        level="default",
    )
    ctx = fake_ctx(
        {},
        model_router=router,
        auth_connector=None,
        logger=None,
        is_tui=False,
    )

    def _once(messages, state, ctx, *, set_previous_response_id=True):
        raise _http_error(500)

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)

    with pytest.raises(RuntimeError) as excinfo:
        run_coro(pr._arequest_backend_canonical([], object(), ctx))

    message = str(excinfo.value)
    assert "/level" in message
    assert "/profiles" in message
    assert "glm-local" in message
    assert router.breaker.is_open("glm-local") is True


def test_begin_turn_main_trace_open_active_alias_raises_honest_class2(
    monkeypatch,
):
    """Important-2: старт хода (`begin_turn`) при открытом активном алиасе кидает
    AliasCircuitOpenError ДО choke-point транспорта — витрина рана должна получить
    честную ошибку класса 2 (модель/endpoint/причина + подсказка /level //profiles),
    а не сырое исключение предохранителя."""
    from gc_core import processing_responses as pr
    from gc_core.model_router import AliasCircuitOpenError

    router = _router(
        {"models": {"glm-local": {}}, "slots": {"default": "glm-local"}},
        level="default",
    )
    router.breaker.trip("glm-local", "5xx после ретраев")
    ctx = fake_ctx(
        {},
        model_router=router,
        auth_connector=None,
        logger=None,
        is_tui=False,
    )

    with pytest.raises(RuntimeError) as excinfo:
        pr.begin_turn_main_trace(_service_state(), ctx)

    assert not isinstance(excinfo.value, AliasCircuitOpenError)
    message = str(excinfo.value)
    assert "/level" in message
    assert "/profiles" in message
    assert "glm-local" in message


def test_begin_turn_main_trace_legacy_is_plain_passthrough():
    """Легаси (пустой реестр) — begin_turn резолвит плоский model без предохранителя;
    обёртка не вмешивается."""
    from gc_core import processing_responses as pr

    router = _router({}, level="default")  # registry_empty → легаси
    ctx = fake_ctx(
        {},
        model_router=router,
        auth_connector=None,
        logger=None,
        is_tui=False,
    )
    state = _service_state(default_model="pre")
    pr.begin_turn_main_trace(state, ctx)
    # легаси model из снимка (None здесь) зафиксирован без ошибки
    assert router.breaker.snapshot() == {}


def test_resolve_check_breaker_false_returns_model_for_open_alias():
    """Important-3: витрины (футер, /status) зовут resolve(check_breaker=False) —
    открытый алиас не роняет резолв, модель показывается честно."""
    router = _router(
        {"models": {"flash": {}}, "slots": {"default": "flash"}},
        level="default",
    )
    router.breaker.trip("flash", "5xx после ретраев")

    resolved = router.resolve("main", check_breaker=False)
    assert resolved.alias == "flash"

    # Дефолтное поведение рантайма (check_breaker=True) — прежнее: мгновенный отказ.
    from gc_core.model_router import AliasCircuitOpenError

    with pytest.raises(AliasCircuitOpenError):
        router.resolve("main")


def test_subagent_ctx_build_both_open_returns_honest_tool_error(tmp_path):
    """Minor-T9: и пин субагента, и активная модель в предохранителе — сборка ctx
    ребёнка не должна ронять сырой AliasCircuitOpenError (ловится выше как
    subagent_ctx_build_failed с сырым str); честная ошибка с внятным сообщением."""
    from gc_core.model_router import AliasCircuitOpenError
    from gc_core.tools.subagents.runner import SubagentRunner

    raw = {
        "models": {"flash": {}, "deepseek-max": {}},
        "slots": {"default": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    }
    cfg = _subagent_cfg(tmp_path, raw)
    parent_router = _router(raw, level="default")
    parent_router.breaker.trip("deepseek-max", "пин недоступен")
    parent_router.breaker.trip("flash", "активная недоступна")
    parent_ctx = _subagent_ctx(cfg, parent_router)
    orch = _subagent_orchestrator_state()
    runner = SubagentRunner(parent_ctx, orch, cfg)

    with pytest.raises(RuntimeError) as excinfo:
        run_coro(runner._build_child_ctx(tool_name="subagent_planner"))

    assert not isinstance(excinfo.value, AliasCircuitOpenError)
    message = str(excinfo.value)
    assert "subagent_planner" in message
    assert "/level" in message or "/profiles" in message


def test_main_trace_open_alias_fails_instantly_without_transport(monkeypatch):
    from gc_core import processing_responses as pr

    router = _router(
        {"models": {"glm-local": {}}, "slots": {"default": "glm-local"}},
        level="default",
    )
    router.breaker.trip("glm-local", "5xx после ретраев")
    ctx = fake_ctx(
        {},
        model_router=router,
        auth_connector=None,
        logger=None,
        is_tui=False,
    )
    calls = {"n": 0}

    def _once(messages, state, ctx, *, set_previous_response_id=True):
        calls["n"] += 1
        return (["msg"], "rid", {})

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)

    with pytest.raises(RuntimeError):
        run_coro(pr._arequest_backend_canonical([], object(), ctx))

    assert calls["n"] == 0


def test_main_trace_legacy_breaker_not_tracked_error_unchanged(monkeypatch):
    """Легаси-режим (registry_empty) — предохранитель не ведётся; исходное
    исключение уходит наверх БЕЗ обёртки/обогащения (как до Task 9)."""
    from gc_core import processing_responses as pr

    router = _router({}, level="default")  # models пуст → registry_empty
    ctx = fake_ctx(
        {},
        model_router=router,
        auth_connector=None,
        logger=None,
        is_tui=False,
    )

    def _once(messages, state, ctx, *, set_previous_response_id=True):
        raise _http_error(500)

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(pr._arequest_backend_canonical([], object(), ctx))

    assert router.breaker.snapshot() == {}


def test_main_trace_context_overflow_not_treated_as_unavailable(monkeypatch):
    """Переполнение контекста — не class-2: исключение проходит НЕИЗМЕНЁННЫМ
    (run_agent делает force-compaction + retry, не честную ошибку)."""
    from gc_core import processing_responses as pr

    router = _router(
        {"models": {"glm-local": {}}, "slots": {"default": "glm-local"}},
        level="default",
    )
    ctx = fake_ctx(
        {},
        model_router=router,
        auth_connector=None,
        logger=None,
        is_tui=False,
    )

    def _once(messages, state, ctx, *, set_previous_response_id=True):
        raise ValueError("maximum context length exceeded")

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)

    with pytest.raises(ValueError, match="maximum context length"):
        run_coro(pr._arequest_backend_canonical([], object(), ctx))

    assert router.breaker.snapshot() == {}


# ---------------------------------------------------------------------------
# Субагент с пином — рестарт (1-й вызов) / деградация (не 1-й) / честная ошибка
# ---------------------------------------------------------------------------


def _subagent_orchestrator_state() -> State:
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


def _subagent_ctx(cfg, router):
    from gc_testkit.helpers.tool_ctx import local_ctx

    ctx = local_ctx(cfg)
    ctx.run_id = "parent_run"
    ctx.auth_connector = None
    ctx.logger = None
    ctx.logging_path = ""
    ctx.summary_trace_path = ""
    ctx.tools_data = {}
    ctx.tools_spec = []
    ctx.tools_description = []
    ctx.is_tui = False
    ctx.model_router = router
    return ctx


def _subagent_cfg(tmp_path, raw):
    """Настройки родителя субагента: реестр ``raw`` + правки прогона; гарды —
    как у ``local_ctx`` (не настроенные тестом выключены)."""
    from gc_testkit.helpers.tool_ctx import tool_settings

    return tool_settings(
        {
            **raw,
            "agent.use_local_subagents": True,
            "backend.api_mode": "chat_completions",
            "agent.subagent_max_steps": 30,
            "paths.skills_data_path": "",
            "agent.system_prompt_id": "test-prompt",
            "logging.save_logs": True,
            "paths.logs_dump_dir": str(tmp_path),
        }
    )


async def test_subagent_first_call_failure_restarts_on_active_model(tmp_path):
    from gc_core.tools.subagents.runner import SubagentRunner

    raw = {
        "models": {"flash": {}, "deepseek-max": {}},
        "slots": {"default": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    }
    cfg = _subagent_cfg(tmp_path, raw)
    parent_router = _router(raw, level="default")
    parent_ctx = _subagent_ctx(cfg, parent_router)
    orch = _subagent_orchestrator_state()
    runner = SubagentRunner(parent_ctx, orch, cfg)

    calls = {"n": 0}

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            assert child_state.steps == 0
            raise _http_error(500)
        # Второй (деградированный) вызов — резолв ребёнка теперь активная модель.
        assert child_ctx.model_router.resolve("main").alias == "flash"
        return "ok"

    result = await runner.arun(
        "subagent_planner", {"task": "plan it", "reason": "r"}, fake_run_agent
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["result"] == "ok"
    assert calls["n"] == 2
    assert parent_router.breaker.is_open("deepseek-max") is True


async def test_subagent_non_first_call_failure_degrades_without_restart(
    tmp_path,
):
    from gc_core.tools.subagents.runner import SubagentRunner

    raw = {
        "models": {"flash": {}, "deepseek-max": {}},
        "slots": {"default": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    }
    cfg = _subagent_cfg(tmp_path, raw)
    parent_router = _router(raw, level="default")
    parent_ctx = _subagent_ctx(cfg, parent_router)
    orch = _subagent_orchestrator_state()
    runner = SubagentRunner(parent_ctx, orch, cfg)

    calls = {"n": 0}
    seeded_trace_message = {"role": "assistant", "content": "partial work"}

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Симулируем: один шаг ребёнка уже отработал успешно (трейс наработан),
            # затем упал следующий вызов.
            child_state.steps = 1
            child_state.messages = child_state.messages + [
                seeded_trace_message
            ]
            raise _http_error(500)
        # Деградация: трейс сохранён (та же child_state.messages передана как batch).
        assert seeded_trace_message in messages
        return "ok"

    result = await runner.arun(
        "subagent_planner", {"task": "plan it", "reason": "r"}, fake_run_agent
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert calls["n"] == 2
    assert parent_router.breaker.is_open("deepseek-max") is True


async def test_subagent_repeated_failure_after_fallback_returns_honest_tool_error(
    tmp_path,
):
    from gc_core.tools.subagents.runner import SubagentRunner

    raw = {
        "models": {"flash": {}, "deepseek-max": {}},
        "slots": {"default": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    }
    cfg = _subagent_cfg(tmp_path, raw)
    parent_router = _router(raw, level="default")
    parent_ctx = _subagent_ctx(cfg, parent_router)
    orch = _subagent_orchestrator_state()
    runner = SubagentRunner(parent_ctx, orch, cfg)

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        raise _http_error(500)

    result = await runner.arun(
        "subagent_planner", {"task": "plan it", "reason": "r"}, fake_run_agent
    )

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "subagent_run_failed"


async def test_subagent_non_availability_error_skips_fallback_entirely(
    tmp_path,
):
    """Небольшой регресс-предохранитель: логическая ошибка (не недоступность
    модели) не должна триггерить рестарт/деградацию — обычная честная ошибка тула."""
    from gc_core.tools.subagents.runner import SubagentRunner

    raw = {
        "models": {"flash": {}, "deepseek-max": {}},
        "slots": {"default": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    }
    cfg = _subagent_cfg(tmp_path, raw)
    parent_router = _router(raw, level="default")
    parent_ctx = _subagent_ctx(cfg, parent_router)
    orch = _subagent_orchestrator_state()
    runner = SubagentRunner(parent_ctx, orch, cfg)

    calls = {"n": 0}

    async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
        calls["n"] += 1
        raise ValueError("some unrelated bug")

    result = await runner.arun(
        "subagent_planner", {"task": "plan it", "reason": "r"}, fake_run_agent
    )

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "subagent_run_failed"
    assert calls["n"] == 1
    assert parent_router.breaker.snapshot() == {}


async def test_unpinned_subagent_failure_trips_breaker_and_propagates_to_main(
    tmp_path, monkeypatch
):
    """Ревью Task 9 (Important): непинованный субагент (нет ``assign.subagents.<name>``,
    резолв через активный уровень) при подтверждённом фейле обязан трипать
    предохранитель — иначе ни один trip-путь не срабатывает и повторные вызовы (этого
    субагента, других субагентов, main) продолжают платить полный backoff (регресс
    §8: «не долбим мёртвый endpoint», «ВСЕ резолвы мгновенно»)."""
    from gc_core import processing_responses as pr
    from gc_core.tools.subagents.runner import SubagentRunner

    raw = {
        "models": {"flash": {}},
        "slots": {"default": "flash"},
    }  # без assign — не пинован
    cfg = _subagent_cfg(tmp_path, raw)
    parent_router = _router(raw, level="default")
    parent_ctx = _subagent_ctx(cfg, parent_router)
    orch = _subagent_orchestrator_state()
    runner = SubagentRunner(parent_ctx, orch, cfg)

    def failing_run_agent(messages, child_state, child_ctx, **kwargs):
        raise _http_error(500)

    result = await runner.arun(
        "subagent_worker", {"task": "do x", "reason": "r"}, failing_run_agent
    )
    payload = json.loads(result)
    assert payload["ok"] is False

    # Предохранитель обязан встать — единственная модель реестра недоступна,
    # деградировать некуда (упавший алиас и есть активная модель).
    assert parent_router.breaker.is_open("flash") is True

    # Повторный вызов ТОГО ЖЕ (непинованного) субагента — мгновенный отказ,
    # ни одного обращения к run_agent_func (транспорт не вызывается вовсе).
    calls = {"n": 0}

    def should_not_be_called(*a, **k):
        calls["n"] += 1
        return "unreachable"

    result2 = await runner.arun(
        "subagent_worker",
        {"task": "do x again", "reason": "r"},
        should_not_be_called,
    )
    payload2 = json.loads(result2)
    assert payload2["ok"] is False
    assert calls["n"] == 0

    # main следующего хода — мгновенный честный класс 2, без обращения в транспорт.
    once_calls = {"n": 0}

    def _once(messages, state, ctx, *, set_previous_response_id=True):
        once_calls["n"] += 1
        return (["msg"], "rid", {})

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", _once)
    with pytest.raises(RuntimeError):
        await pr._arequest_backend_canonical([], object(), parent_ctx)
    assert once_calls["n"] == 0


def test_subagent_pin_already_open_redirects_to_active_at_ctx_build_time(
    tmp_path,
):
    """Пин этого субагента уже уронен другим вызовом этой сессии — ребёнок сразу
    собирается на активной модели (без повторной попытки на заведомо мёртвом
    пине), см. `SubagentRunner._build_child_model_router`."""
    from gc_core.tools.subagents.runner import SubagentRunner

    raw = {
        "models": {"flash": {}, "deepseek-max": {}},
        "slots": {"default": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    }
    cfg = _subagent_cfg(tmp_path, raw)
    parent_router = _router(raw, level="default")
    parent_router.breaker.trip("deepseek-max", "5xx после ретраев")
    parent_ctx = _subagent_ctx(cfg, parent_router)
    orch = _subagent_orchestrator_state()
    runner = SubagentRunner(parent_ctx, orch, cfg)

    child_ctx = run_coro(runner._build_child_ctx(tool_name="subagent_planner"))

    assert child_ctx.model_router.resolve("main").alias == "flash"


# ---------------------------------------------------------------------------
# AliasCircuitOpenError — распознаётся как недоступность (единая точка правды)
# ---------------------------------------------------------------------------


def test_alias_circuit_open_error_is_classified_as_model_unavailable():
    from gc_core.model_router import AliasCircuitOpenError, ResolvedModel
    from gc_core.utils.backend_errors import is_model_unavailable_error

    resolved = ResolvedModel(
        alias="flash",
        model="flash-model",
        url="http://flash.local/v1",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )
    exc = AliasCircuitOpenError(resolved, "5xx после ретраев")
    assert is_model_unavailable_error(exc) is True
