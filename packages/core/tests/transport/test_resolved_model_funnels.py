"""Транспортный шов (Task 6, план «Профили v2, волна A»).

Обе воронки (`utils.connect_backend` / `utils.llm_chat_client.request_chat_completion_text`)
принимают `ResolvedModel` явным параметром; каждый служебный LLM-вызов объявляет свой
`consumer` id и резолвится через `ctx.model_router.resolve(consumer)`. См.
.superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-6-brief.md.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from gc_core.model_router import ResolvedModel
from gc_core.utils import llm_chat_client
from gc_core.utils.connect_backend import (
    acreate_call_headers,
    build_call_headers,
    create_call_data_completions,
    create_call_data_responses,
)
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx, make_settings


def _canonical_once(processing_module, *args, **kwargs):
    """Позвать канонический шов запроса к бэкенду.

    Шов приватный и асинхронный: синхронные двойники в этой ветке заведены
    только для публичных входов (``run_agent``, ``process_user_ask`` и т.п.),
    заводить ещё один ради теста — значит держать вторую реализацию.
    """
    import asyncio

    return asyncio.run(
        processing_module._arequest_backend_canonical_once(*args, **kwargs)
    )


def _serve_main_trace(router) -> None:
    """Main-трейс берёт модель через `ModelRouter.resolve_main_at` (уровень
    вызова). У фейка без уровней это тот же `resolve("main")` — счёт вызовов
    `resolve` в проверках остаётся прежним."""
    router.resolve_main_at = lambda level, **_kw: router.resolve("main")


def _resolved(**overrides) -> ResolvedModel:
    base = dict(
        alias="light",
        model="light-model",
        url="http://alias.local/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )
    base.update(overrides)
    return ResolvedModel(**base)


def _state(**overrides) -> SimpleNamespace:
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


# ---------------------------------------------------------------------------
# 1) Воронка строит запрос из ResolvedModel: model / url / authorization (3 варианта)
# ---------------------------------------------------------------------------


async def test_create_call_headers_authorization_none_inherits_effective_header():
    ctx = fake_ctx(
        {"backend.api_mode": "chat_completions", "agent.x_cl_id": "1"},
        auth_connector=SimpleNamespace(get_token=lambda: "tok-123"),
    )
    headers = await acreate_call_headers(
        _state(), ctx, resolved=_resolved(authorization=None)
    )
    assert headers["Authorization"] == "Bearer tok-123"


def test_create_call_headers_authorization_empty_string_omits_header():
    ctx = fake_ctx(
        {"backend.api_mode": "chat_completions", "agent.x_cl_id": "1"},
        auth_connector=SimpleNamespace(get_token=lambda: "tok-123"),
    )
    headers = build_call_headers(
        _state(), ctx, resolved=_resolved(authorization="")
    )
    assert "Authorization" not in headers


def test_create_call_headers_authorization_value_used_as_is():
    ctx = fake_ctx(
        {"backend.api_mode": "chat_completions", "agent.x_cl_id": "1"},
        auth_connector=SimpleNamespace(get_token=lambda: "tok-123"),
    )
    headers = build_call_headers(
        _state(), ctx, resolved=_resolved(authorization="Bearer model-token")
    )
    assert headers["Authorization"] == "Bearer model-token"


async def test_create_call_headers_responses_mode_ignores_resolved_authorization():
    # спека §4: per-model token/url/backend-квирки — chat_completions-only.
    ctx = fake_ctx(
        {"backend.api_mode": "responses", "agent.x_cl_id": "1"},
        auth_connector=SimpleNamespace(get_token=lambda: "tok-123"),
    )
    headers = await acreate_call_headers(
        _state(), ctx, resolved=_resolved(authorization="Bearer model-token")
    )
    assert headers["Authorization"] == "Bearer tok-123"


def test_create_call_headers_model_token_survives_global_chat_completion_headers():
    # Боевой конфиг (config.default.yml) держит Authorization в глобальном
    # backend.chat_completion_headers. Оверлей общих заголовков НЕ должен
    # перебивать per-model token — иначе корп-токен уезжает на чужой хост.
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.x_cl_id": "1",
            "backend.chat_completion_headers": {
                "Authorization": "Bearer corp-token",
                "X-Custom": "keep-me",
            },
        },
        auth_connector=None,
    )
    headers = build_call_headers(
        _state(), ctx, resolved=_resolved(authorization="Bearer model-token")
    )
    assert headers["Authorization"] == "Bearer model-token"
    assert headers["X-Custom"] == "keep-me"


def test_create_call_headers_empty_token_not_revived_by_global_headers():
    # authorization="" — «явно без Authorization» (напр. локальная модель).
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.x_cl_id": "1",
            "backend.chat_completion_headers": {
                "Authorization": "Bearer corp-token"
            },
        },
        auth_connector=SimpleNamespace(get_token=lambda: "tok-123"),
    )
    headers = build_call_headers(
        _state(), ctx, resolved=_resolved(authorization="")
    )
    assert "Authorization" not in headers


def test_create_call_headers_lowercase_global_authorization_also_yields_to_model_token():
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.x_cl_id": "1",
            "backend.chat_completion_headers": {
                "authorization": "Bearer corp-token"
            },
        },
        auth_connector=None,
    )
    headers = build_call_headers(
        _state(), ctx, resolved=_resolved(authorization="Bearer model-token")
    )
    assert headers["Authorization"] == "Bearer model-token"
    assert "authorization" not in headers


async def test_create_call_headers_global_authorization_applies_without_model_token():
    # Охранный: без per-model решения (authorization=None) поведение прежнее —
    # статический заголовок конфига выигрывает у токена коннектора.
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.x_cl_id": "1",
            "backend.chat_completion_headers": {
                "Authorization": "Bearer corp-token"
            },
        },
        auth_connector=SimpleNamespace(get_token=lambda: "tok-123"),
    )
    headers = await acreate_call_headers(
        _state(), ctx, resolved=_resolved(authorization=None)
    )
    assert headers["Authorization"] == "Bearer corp-token"


def test_create_call_headers_model_backend_overlay_authorization_wins_over_token():
    # Оверлей заголовков НА ТОЙ ЖЕ модели — тоже per-model решение, и более
    # конкретное: он уточняет token, а не перебивается им.
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.x_cl_id": "1",
            "backend.chat_completion_headers": {
                "Authorization": "Bearer corp-token"
            },
        },
        auth_connector=None,
    )
    headers = build_call_headers(
        _state(),
        ctx,
        resolved=_resolved(
            authorization="Bearer model-token",
            backend_overrides={
                "chat_completion_headers": {"Authorization": "Bearer overlay"}
            },
        ),
    )
    assert headers["Authorization"] == "Bearer overlay"


def test_create_call_data_completions_uses_resolved_model():
    data = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        _state(default_model="legacy-model"),
        resolved=_resolved(model="light-model"),
    )
    assert data["model"] == "light-model"


def test_create_call_data_responses_uses_resolved_model():
    data = create_call_data_responses(
        [{"role": "user", "content": "hi"}],
        _state(default_model="legacy-model"),
        resolved=_resolved(model="light-model"),
    )
    assert data["model"] == "light-model"


def test_request_chat_completion_text_uses_resolved_url_and_model(monkeypatch):
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured["url"] = kwargs["url"]
        captured["payload"] = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "ok"}}]}, captured[
            "payload"
        ]

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    monkeypatch.setattr(
        llm_chat_client,
        "acreate_call_headers",
        as_coro(lambda _s, _c, resolved=None: {}),
    )

    router = SimpleNamespace(
        resolve=Mock(
            return_value=_resolved(
                model="light-model",
                url="http://alias.local/v1/chat/completions",
            )
        )
    )
    ctx = fake_ctx(
        {
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
        },
        model_router=router,
        logger=None,
    )
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_state(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="summary",
        )
    )
    assert captured["url"] == "http://alias.local/v1/chat/completions"
    assert captured["payload"]["model"] == "light-model"
    router.resolve.assert_called_once_with("summary")


# ---------------------------------------------------------------------------
# 2) backend_overrides применяются поверх эффективных backend-квирков
# ---------------------------------------------------------------------------


def test_create_call_data_completions_backend_overrides_apply_on_top():
    data = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        _state(),
        settings=make_settings(
            {
                "backend.use_max_tokens_param": False,
                "backend.chat_completions_max_tokens": 100,
                "backend.chat_completion_kwargs": {"temperature": 0.1},
            }
        ),
        resolved=_resolved(
            backend_overrides={
                "use_max_tokens_param": True,
                "chat_completions_max_tokens": 55,
                "chat_completion_kwargs": {"temperature": 0.9},
            }
        ),
    )
    assert data["max_tokens"] == 55
    assert "max_completion_tokens" not in data
    # Kwargs живут в main, не в backend алиаса: оверрайд backend их не подменяет.
    assert data["temperature"] == 0.1


def test_create_call_headers_backend_overrides_chat_completion_headers():
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.chat_completion_headers": {"X-Base": "base"},
        },
        auth_connector=None,
    )
    headers = build_call_headers(
        _state(),
        ctx,
        resolved=_resolved(
            backend_overrides={
                "chat_completion_headers": {"X-Model": "model-value"}
            }
        ),
    )
    assert headers["X-Model"] == "model-value"
    assert "X-Base" not in headers


def test_request_chat_completion_text_backend_overrides_apply_to_transport_params(
    monkeypatch,
):
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured.update(kwargs)
        payload = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "ok"}}]}, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    monkeypatch.setattr(
        llm_chat_client,
        "acreate_call_headers",
        as_coro(lambda _s, _c, resolved=None: {}),
    )

    router = SimpleNamespace(
        resolve=Mock(
            return_value=_resolved(
                backend_overrides={"use_ssl": False, "request_timeout_sec": 42}
            )
        )
    )
    ctx = fake_ctx(
        {
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
            "backend.use_ssl": True,
        },
        model_router=router,
        logger=None,
    )
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_state(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="summary",
        )
    )
    assert captured["verify"] is False
    assert captured["timeout"] == 42.0


# ---------------------------------------------------------------------------
# 3) Каждый служебный вызов объявляет свой consumer (router.resolve(consumer))
# ---------------------------------------------------------------------------

_FINISH_GATE_JSON = json.dumps(
    {
        "action": "accept",
        "trace_ok": True,
        "report_ok": True,
        "is_question": False,
        "trace_gaps": [],
        "report_mismatches": [],
    }
)
_JUDGE_JSON = json.dumps(
    {"verdict": "safe", "reasons": ["ok"], "risk_flags": [], "confidence": 0.9}
)


def _router_ctx(**cfg_overrides):
    router = SimpleNamespace(resolve=Mock(return_value=_resolved()))
    cfg = {
        "backend.completions_url": "http://backend.local/v1/chat/completions",
        "backend.request_timeout_sec": 600,
        "backend.api_mode": "chat_completions",
    }
    cfg.update(cfg_overrides)
    ctx = fake_ctx(
        cfg,
        model_router=router,
        auxiliary_llm=SimpleNamespace(
            max_tokens=500, judge_max_tokens=200, completion_kwargs={}
        ),
        logger=None,
        auth_connector=None,
        is_tui=False,
    )
    return ctx, router


def _patch_llm_transport(monkeypatch, response_text: str) -> list:
    captured: list = []

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        payload = payload_factory(initial_max_tokens)
        captured.append(payload)
        return {"choices": [{"message": {"content": response_text}}]}, payload

    # Форма у воронки одна — корутинная; синхронного имени в модуле нет.
    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    return captured


async def test_command_judge_declares_own_consumer(monkeypatch):
    """Судья теперь корутинный: его ждёт loop вызывающего, а не свой одноразовый."""
    from gc_core.security import command_moderation

    _patch_llm_transport(monkeypatch, _JUDGE_JSON)
    ctx, router = _router_ctx()
    judge_client = command_moderation._default_judge_client(
        (), {"ctx": ctx, "state": _state()}
    )
    result = await judge_client({"tool_name": "execute_command"})
    assert result["verdict"] == "safe"
    router.resolve.assert_called_once_with("command_judge")


def test_memory_compaction_declares_own_consumer(monkeypatch):
    from gc_core.project_memory import compaction

    _patch_llm_transport(monkeypatch, "compressed text")
    ctx, router = _router_ctx()
    res = run_coro(compaction.acompact_text("long text", ctx, _state()))
    assert res.ok is True
    router.resolve.assert_called_once_with("memory_compaction")


def test_finish_gate_declares_own_consumer(monkeypatch):
    from gc_core.intent_guard.final_report_consistency import (
        avalidate_final_report_vs_user_intent,
    )
    from gc_core.intent_guard.trace_digest import FinishGateTraceDigest

    _patch_llm_transport(monkeypatch, _FINISH_GATE_JSON)
    ctx, router = _router_ctx()
    digest = FinishGateTraceDigest(
        trace_digest_text="<message>...</message>", digest_chars=20
    )
    result = run_coro(
        avalidate_final_report_vs_user_intent(
            ctx=ctx,
            state=_state(),
            current_user_requests=["do x"],
            finish_handoff_arguments="done",
            trace_digest=digest,
        )
    )
    assert result.action == "accept"
    router.resolve.assert_called_once_with("finish_gate")


def test_text_answer_judge_declares_own_consumer_not_finish_gate(monkeypatch):
    # спека §5.1: text_answer_judge переиспользует воронку finish_gate, но
    # объявляется СВОИМ id — не должен резолвиться как "finish_gate".
    from gc_core.intent_guard.text_answer_judge import ajudge_text_answer

    _patch_llm_transport(monkeypatch, _FINISH_GATE_JSON)
    ctx, router = _router_ctx()
    state = _state(
        current_user_intent=[], user_intent_log=[], parent_state_id=None
    )
    result = run_coro(ajudge_text_answer(ctx, state, "the final answer text"))
    assert result is not None
    assert result.action == "accept"
    router.resolve.assert_called_once_with("text_answer_judge")


def test_intent_scope_tool_summary_declares_own_consumer(monkeypatch):
    from gc_core.intent_scope import summarizer

    _patch_llm_transport(monkeypatch, "a summary line")
    ctx, router = _router_ctx()
    line = run_coro(
        summarizer.asummarize_tool_intent_line(
            ctx=ctx,
            state=_state(current_user_intent=[]),
            tool_name="turn_ask_human",
            tool_arguments="{}",
            tool_output_raw="raw tool output",
        )
    )
    assert line == "a summary line"
    router.resolve.assert_called_once_with("intent_scope")


def test_intent_scope_final_answer_declares_own_consumer(monkeypatch):
    from gc_core.intent_scope import summarizer

    _patch_llm_transport(monkeypatch, "final answer summary")
    ctx, router = _router_ctx()
    line = run_coro(
        summarizer.asummarize_final_answer_line(
            ctx=ctx,
            state=_state(current_user_intent=[]),
            assistant_text="a very long final answer" * 5,
        )
    )
    assert line == "final answer summary"
    router.resolve.assert_called_once_with("intent_scope")


def test_summary_compress_declares_own_consumer(monkeypatch):
    from gc_core.summarize.summary_llm_steps import acompress_summary_text
    from gc_core.summarize.summary_spec import assemble_summary_xml

    short = assemble_summary_xml({"task_and_context": "short"})
    _patch_llm_transport(monkeypatch, short)
    ctx, router = _router_ctx()
    out = run_coro(
        acompress_summary_text(
            ctx=ctx,
            state=_state(),
            summary_text=assemble_summary_xml({"task_and_context": "longer"}),
            max_chars=len("short"),
            max_tokens=50,
        )
    )
    assert out == short
    router.resolve.assert_called_once_with("summary")


def test_summary_repair_declares_own_consumer(monkeypatch):
    from gc_core.summarize.summary_llm_steps import arepair_summary_text

    _patch_llm_transport(monkeypatch, "<summary>fixed</summary>")
    ctx, router = _router_ctx()
    out = run_coro(
        arepair_summary_text(
            ctx=ctx,
            state=_state(),
            history_text="history",
            persistent_state_prefix="",
            invalid_summary="broken",
            reason="missing tags",
            max_tokens=50,
        )
    )
    assert out == "<summary>fixed</summary>"
    router.resolve.assert_called_once_with("summary")


def test_memory_patch_replace_declares_own_consumer(monkeypatch):
    from gc_core.summarize.memory_suggestions import arequest_patch_replace

    _patch_llm_transport(monkeypatch, "<goal>x</goal>")
    ctx, router = _router_ctx()
    result = run_coro(
        arequest_patch_replace(
            ctx=ctx, state=_state(), history_text="hist", summary_text="sum"
        )
    )
    assert result.raw_response == "<goal>x</goal>"
    router.resolve.assert_called_once_with("summary")


def test_memory_deltas_format_repair_declares_own_consumer(monkeypatch):
    from gc_core.summarize.memory_suggestions import (
        arequest_memory_deltas_format_repair,
    )

    _patch_llm_transport(monkeypatch, "<goal>repaired</goal>")
    ctx, router = _router_ctx()
    out = run_coro(
        arequest_memory_deltas_format_repair(
            ctx=ctx, state=_state(), raw_output="broken"
        )
    )
    assert out == "<goal>repaired</goal>"
    router.resolve.assert_called_once_with("summary")


def test_thread_summarizer_batch_declares_summary_consumer(monkeypatch):
    """summarize_dialog_via_chat_completions — тяжёлый оркестратор; проверяем факт
    объявления consumer="summary" напрямую на вызове request_summary_text (тот же
    паттерн монкипатча, что и в tests/test_thread_summarizer_incremental.py), не
    поднимая весь LLM-транспорт по-настоящему."""
    from gc_core.summarize.thread_summarizer import (
        asummarize_dialog_via_chat_completions,
    )
    from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests

    captured = {}

    def _fake_request_summary_text(*, consumer=None, **kwargs):
        captured["consumer"] = consumer
        return valid_summary_xml_for_tests(seed="batch summary")

    # summary_llm_steps имеет свою привязанную ссылку на request_summary_text
    # (repair-путь вызывается независимо от thread_summarizer) — патчим обе.
    monkeypatch.setattr(
        "gc_core.summarize.thread_summarizer.arequest_summary_text",
        as_coro(_fake_request_summary_text),
    )
    monkeypatch.setattr(
        "gc_core.summarize.summary_llm_steps.arequest_summary_text",
        as_coro(_fake_request_summary_text),
    )
    monkeypatch.setattr(
        "gc_core.summarize.prompt_loading.aload_summary_prompt",
        as_coro(lambda _store: "prompt"),
    )
    monkeypatch.setattr(
        "gc_core.summarize.memory_suggestions.arequest_patch_replace",
        as_coro(
            lambda **_k: SimpleNamespace(request_messages=[], raw_response="")
        ),
    )

    ctx = fake_ctx(
        {
            "agent.model": "test-model",
            "backend.completions_url": "http://example.test/v1/chat/completions",
            "summary": {
                "chat_completions_summary_trigger_chars": 999999,
                "incremental_batch_chars": int(999999 * 1.2),
                "max_accumulated_summary_chars": 20000,
                "last_dialog_steps_count": 5,
                "last_dialog_step_message_max_chars": 5000,
            },
            "chat_completions": {
                "auxiliary_llm": {"max_tokens": 8192, "completion_kwargs": {}},
            },
        },
        logger=None,
    )
    state = SimpleNamespace(default_model="test-model")
    history = [
        {"type": "message", "role": "user", "content": "first user"},
        {"type": "message", "role": "assistant", "content": "first assistant"},
    ]

    result = run_coro(
        asummarize_dialog_via_chat_completions(
            ctx=ctx,
            state=state,
            full_history_messages=history,
        )
    )
    assert captured["consumer"] == "summary"
    assert "batch summary" in result["raw_summary"]


def test_main_trace_resolves_and_declares_main_consumer(monkeypatch):
    from gc_core import processing_responses as processing_module

    def _fake_request_chat_completions_with_policy(
        *, payload_factory, initial_max_tokens, **kwargs
    ):
        payload = payload_factory(initial_max_tokens)
        response = {
            "id": "chat_1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        return response, payload

    monkeypatch.setattr(
        processing_module,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_request_chat_completions_with_policy),
    )

    router = SimpleNamespace(
        resolve=Mock(return_value=_resolved(model="light-model"))
    )
    _serve_main_trace(router)
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
        },
        model_router=router,
        auth_connector=None,
        logger=None,
    )
    state = _state(default_model="legacy-model")

    resolved = processing_module.resolve_main_for_call(state, ctx)
    _canonical_once(
        processing_module,
        [{"role": "user", "content": "hi"}],
        state,
        ctx,
        resolved=resolved,
    )

    # main фиксируется один раз; vision резолвится для свежего ключа доступа
    # и затем чистого решения compute_audience (без сетевой пробы).
    assert [c.args for c in router.resolve.call_args_list] == [
        ("main",),
        ("vision",),
        ("vision",),
    ]


def test_main_trace_backend_overrides_apply_to_network_params(monkeypatch):
    """Корп-модель реестра (use_ssl=False + cert_path/key_path + request_timeout_sec)
    активна на уровне main — сетевые параметры запроса берутся из backend_overrides,
    не из дефолтного cfg (важная находка ревью Task 6)."""
    from gc_core import processing_responses as processing_module

    captured = {}

    def _fake_request_chat_completions_with_policy(
        *, payload_factory, initial_max_tokens, **kwargs
    ):
        captured.update(kwargs)
        payload = payload_factory(initial_max_tokens)
        response = {
            "id": "chat_1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        return response, payload

    monkeypatch.setattr(
        processing_module,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_request_chat_completions_with_policy),
    )

    router = SimpleNamespace(
        resolve=Mock(
            return_value=_resolved(
                model="corp-model",
                backend_overrides={
                    "use_ssl": False,
                    "cert_path": "corp-cert.pem",
                    "key_path": "corp-key.pem",
                    "request_timeout_sec": 42,
                },
            )
        )
    )
    _serve_main_trace(router)
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
        },
        model_router=router,
        auth_connector=None,
        logger=None,
    )
    state = _state(default_model="legacy-model")

    resolved = processing_module.resolve_main_for_call(state, ctx)
    _canonical_once(
        processing_module,
        [{"role": "user", "content": "hi"}],
        state,
        ctx,
        resolved=resolved,
    )

    assert captured["verify"] is False
    assert captured["cert"] == ("corp-cert.pem", "corp-key.pem")
    assert captured["timeout"] == 42.0


def test_main_trace_legacy_network_params_without_overrides(monkeypatch):
    """Легаси-регресс: без ctx.model_router (или без overrides) сетевые параметры
    main-вызова — байт-в-байт из cfg, как до Task 6/ревью."""
    from gc_core import processing_responses as processing_module

    captured = {}

    def _fake_request_chat_completions_with_policy(
        *, payload_factory, initial_max_tokens, **kwargs
    ):
        captured.update(kwargs)
        payload = payload_factory(initial_max_tokens)
        response = {
            "id": "chat_1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        return response, payload

    monkeypatch.setattr(
        processing_module,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_request_chat_completions_with_policy),
    )

    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
        },
        auth_connector=None,
        logger=None,
    )
    state = _state(default_model="legacy-model")

    _canonical_once(
        processing_module,
        [{"role": "user", "content": "hi"}],
        state,
        ctx,
        resolved=None,
    )

    assert captured["verify"] is True
    assert captured["cert"] is None
    assert captured["timeout"] == 600.0
    assert captured["url"] == "http://legacy.local/v1/chat/completions"


def test_main_trace_responses_mode_ignores_network_overrides(monkeypatch):
    """Спека §4: per-model сетевые квирки — chat_completions-only; в responses-режиме
    main-вызов игнорирует backend_overrides даже если Router их вернул."""
    from gc_core import processing_responses as processing_module

    captured = {}

    def _fake_request_backend(
        call_data,
        headers,
        *,
        url,
        verify,
        cert,
        use_retry,
        logger,
        timeout,
        config_proxy,
    ):
        captured.update(
            {
                "verify": verify,
                "cert": cert,
                "timeout": timeout,
                "config_proxy": config_proxy,
            }
        )
        return {"id": "resp_1", "output": []}, {}

    monkeypatch.setattr(
        processing_module, "arequest_backend", as_coro(_fake_request_backend)
    )

    router = SimpleNamespace(
        resolve=Mock(
            return_value=_resolved(
                model="corp-model",
                backend_overrides={
                    "use_ssl": False,
                    "request_timeout_sec": 42,
                },
            )
        )
    )
    _serve_main_trace(router)
    ctx = fake_ctx(
        {
            "backend.api_mode": "responses",
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
        },
        model_router=router,
        auth_connector=None,
        logger=None,
    )
    state = _state(default_model="legacy-model")

    resolved = processing_module.resolve_main_for_call(state, ctx)
    _canonical_once(
        processing_module,
        [{"role": "user", "content": "hi"}],
        state,
        ctx,
        resolved=resolved,
    )

    assert captured["verify"] is True
    assert captured["timeout"] == 600.0


# ---------------------------------------------------------------------------
# 4) Легаси-регресс: без реестра (resolved=None) — воронки читают cfg как раньше
# ---------------------------------------------------------------------------


def test_legacy_create_call_data_completions_keeps_state_default_model_without_resolved():
    data = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        _state(default_model="legacy-model"),
        settings=make_settings({"backend.chat_completions_max_tokens": 999}),
    )
    assert data["model"] == "legacy-model"
    assert data["max_completion_tokens"] == 999


def test_legacy_create_call_data_responses_keeps_state_default_model_without_resolved():
    data = create_call_data_responses(
        [{"role": "user", "content": "hi"}],
        _state(default_model="legacy-model"),
    )
    assert data["model"] == "legacy-model"


async def test_legacy_create_call_headers_uses_auth_connector_without_resolved():
    ctx = fake_ctx(
        {"backend.api_mode": "chat_completions", "agent.x_cl_id": "7"},
        auth_connector=SimpleNamespace(get_token=lambda: "legacy-token"),
    )
    headers = await acreate_call_headers(_state(), ctx)
    assert headers["Authorization"] == "Bearer legacy-token"


def test_legacy_request_chat_completion_text_without_model_router(monkeypatch):
    """ctx без model_router (легаси/тестовый двойник) — resolved=None, router.resolve
    не вызывается вовсе; запрос строится из cfg/state как до Task 6."""
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured["url"] = kwargs["url"]
        captured["payload"] = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "ok"}}]}, captured[
            "payload"
        ]

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    monkeypatch.setattr(
        llm_chat_client,
        "acreate_call_headers",
        as_coro(lambda _s, _c, resolved=None: {}),
    )

    ctx = fake_ctx(
        {
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
        },
        logger=None,
    )
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_state(default_model="legacy-model"),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="summary",
        )
    )
    assert captured["url"] == "http://legacy.local/v1/chat/completions"
    assert captured["payload"]["model"] == "legacy-model"


def test_main_trace_backend_overrides_reach_retry_policy_and_token_limits(
    monkeypatch,
):
    """Секция модели принимает ВЕСЬ BackendConfig, поэтому в ней законны и
    `retry_embedded_choice_errors`, и лимиты токенов. Читались они плоско из cfg —
    и модель с маленьким окном получала запрос от чужого потолка, а тюнинг ретрая
    под конкретный прокси не применялся вовсе (соседний use_retry при этом
    оверрайд уважал)."""
    from gc_core import processing_responses as processing_module

    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured.update(kwargs)
        captured["initial_max_tokens"] = initial_max_tokens
        payload_factory(initial_max_tokens)
        return {
            "id": "chat_1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
        }, {}

    monkeypatch.setattr(
        processing_module,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )

    router = SimpleNamespace(
        resolve=Mock(
            return_value=_resolved(
                model="tiny-model",
                backend_overrides={
                    "retry_embedded_choice_errors": False,
                    "chat_completions_max_tokens": 4096,
                    "chat_completions_min_response_tokens": 256,
                },
            )
        )
    )
    _serve_main_trace(router)
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
            "backend.retry_embedded_choice_errors": True,
            "backend.chat_completions_max_tokens": 32768,
            "backend.chat_completions_min_response_tokens": 1024,
        },
        model_router=router,
        auth_connector=None,
        logger=None,
    )

    state = _state(default_model="legacy-model")
    resolved = processing_module.resolve_main_for_call(state, ctx)
    _canonical_once(
        processing_module,
        [{"role": "user", "content": "hi"}],
        state,
        ctx,
        resolved=resolved,
    )

    assert captured["retry_embedded_choice_errors"] is False
    assert captured["initial_max_tokens"] == 4096
    assert captured["min_response_tokens"] == 256


def test_service_funnel_backend_override_reaches_retry_policy(monkeypatch):
    """То же для служебной воронки: единственное поле backend, которое она читала
    мимо оверрайдов, — ретрай зашитой ошибки."""
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured.update(kwargs)
        payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "ok"}}]}, {}

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    monkeypatch.setattr(
        llm_chat_client,
        "acreate_call_headers",
        as_coro(lambda _s, _c, resolved=None: {}),
    )

    router = SimpleNamespace(
        resolve=Mock(
            return_value=_resolved(
                backend_overrides={"retry_embedded_choice_errors": False}
            )
        )
    )
    ctx = fake_ctx(
        {
            "backend.completions_url": "http://legacy.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
            "backend.retry_embedded_choice_errors": True,
        },
        model_router=router,
        logger=None,
    )
    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_state(),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            consumer="summary",
        )
    )

    assert captured["retry_embedded_choice_errors"] is False


# ---------------------------------------------------------------------------
# 4) Сторож: каждое поле BackendConfig классифицировано
# ---------------------------------------------------------------------------

# `models.<alias>.backend` — это ВЕСЬ BackendConfig, поэтому любое поле, которое
# добавят в схему, секция модели примет молча. Если рантайм его не читает, ключ
# принимается и не работает — без ошибки, без предупреждения. Так уже приехали
# retry_embedded_choice_errors и send_tool_strict (синк с develop 2026-08-20).
# Сторож заставляет решать судьбу нового поля в момент его появления.

# Читаются из resolved.backend_overrides по факту вызова.
_HONORED_PER_MODEL = {
    "cert_path",
    "key_path",  # utils/connect_backend.effective_cert_config
    "chat_completion_headers",  # create_call_headers
    "chat_completions_max_tokens",  # потолок адаптивного цикла
    "chat_completions_min_response_tokens",  # пол адаптивного цикла
    "completions_url",  # model_router: адрес модели
    "claude_prompt_cache",  # разметка prompt cache: create_call_data_completions
    "proxy",
    "request_timeout_sec",
    "use_retry",
    "use_ssl",  # resolve_network_params
    "retry_embedded_choice_errors",  # там же
    "use_max_tokens_param",  # выбор max_tokens/max_completion_tokens
    "openai_strict_tools_schema",  # проекция схемы тулов: create_call_data_completions
    "send_tool_strict",  # то же: снимать ли function.strict с копии на отправке
    "vision",  # vision/audience: модель заявлена слепой
}

# Остаются глобальными ОСОЗНАННО — с причиной у каждого.
_GLOBAL_ONLY = {
    "chat_completion_kwargs": (
        "глобальный dict для main без своей секции; kwargs алиаса — "
        "models.<alias>.main и auxiliary, backend алиаса их не несёт"
    ),
    "api_mode": "режим прогона: responses/chat_completions выбирается один раз на ран",
    "stream": "флаг TUI-стрима, к модели отношения не имеет",
}


def test_every_backend_field_is_classified_per_model_or_global():
    """Новое поле BackendConfig обязано попасть в один из двух списков. Падение
    здесь — не поломка, а вопрос: работает ли ключ в секции модели? Если да —
    прочитать его в воронке и внести в _HONORED_PER_MODEL; если нет — записать
    в _GLOBAL_ONLY с причиной, чтобы ограничение было названо вслух."""
    from gc_core.config_modules.backend import BackendConfig

    schema_fields = set(BackendConfig.model_fields)
    unclassified = schema_fields - _HONORED_PER_MODEL - set(_GLOBAL_ONLY)
    assert not unclassified, (
        f"поля backend без решения про per-model: {sorted(unclassified)}. "
        "Секция модели их уже принимает — либо прочитайте в воронке, либо "
        "назовите глобальными с причиной"
    )
    stale = (_HONORED_PER_MODEL | set(_GLOBAL_ONLY)) - schema_fields
    assert not stale, (
        f"в списках есть поля, которых в схеме больше нет: {sorted(stale)}"
    )


def test_fields_declared_per_model_are_actually_read_by_the_funnels():
    """Заявить поле per-model мало — его должен кто-то прочитать. Ищем имя в
    модулях, которые накладывают оверрайды; отсутствие означает, что ключ
    принимается схемой и молча теряется."""
    from gc_testkit.paths import package_dir

    # Пути к исходникам считаем от раскладки пакетов, а не от cwd: плоского
    # <repo>/src больше нет, а прогон возможен и из packages/core.
    core = package_dir("gc_core")
    readers = [
        core / "utils" / "connect_backend.py",
        core / "utils" / "llm_chat_client.py",
        core / "processing_responses.py",
        core / "model_router.py",
        core / "vision" / "audience.py",
    ]
    text = "\n".join(p.read_text(encoding="utf-8") for p in readers)
    missing = sorted(f for f in _HONORED_PER_MODEL if f'"{f}"' not in text)
    assert not missing, (
        f"заявлены per-model, но не читаются ни одной воронкой: {missing}"
    )


def test_openai_strict_schema_is_read_per_model_of_the_call(tmp_path):
    """Спека auto-routing §5.2 п.4: модель с `backend.openai_strict_tools_schema: true`
    получает строгую схему инструментов, соседняя без ключа — обычную, в одном ране."""
    from gc_core import processing_responses as pr
    from gc_core import run_context as rc
    from gc_core.config_modules.root import AppConfig

    raw = {
        "models": {
            "strict-one": {"backend": {"openai_strict_tools_schema": True}},
            "loose-one": {},
        },
        "slots": {"light": "loose-one", "max": "strict-one"},
        "backend": {"api_mode": "chat_completions"},
        "logging": {"save_logs": False},
    }
    cfg = AppConfig.model_validate(raw)
    level = {"value": "light"}
    ctx = run_coro(
        rc.acreate_run_context(cfg, choice_provider=lambda: level["value"])
    )
    state = _state(default_model="legacy-model", tools=ctx.tools_spec)

    loose = pr.resolve_main_for_call(state, ctx)
    canonical_loose = ctx.tools_data["fs_read_file"]["spec"]
    loose_data = create_call_data_completions(
        [], state, settings=cfg, resolved=loose
    )
    level["value"] = "max"
    strict = pr.resolve_main_for_call(state, ctx)
    canonical_strict = ctx.tools_data["fs_read_file"]["spec"]
    strict_data = create_call_data_completions(
        [], state, settings=cfg, resolved=strict
    )

    def read_spec(data):
        return next(
            t["function"]
            for t in data["tools"]
            if t["function"]["name"] == "fs_read_file"
        )

    assert state.tools is ctx.tools_spec
    strict_params = read_spec(strict_data)["parameters"]
    loose_params = read_spec(loose_data)["parameters"]
    assert set(strict_params["required"]) == set(strict_params["properties"])
    assert set(loose_params["required"]) < set(loose_params["properties"])
    assert strict_params["additionalProperties"] is False
    assert "x-openai-strict" not in canonical_loose["parameters"]
    assert "x-openai-strict" not in canonical_strict["parameters"]


def test_openai_strict_schema_per_model_override_is_ignored_in_responses_mode():
    tools = [
        {
            "name": "read",
            "type": "function",
            "description": "read",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "optional": {"type": "string"},
                },
                "required": ["path"],
            },
        }
    ]
    state = _state(tools=tools)
    settings = make_settings({"backend.api_mode": "responses"})
    data = create_call_data_responses(
        [],
        state,
        settings=settings,
        resolved=_resolved(
            backend_overrides={"openai_strict_tools_schema": True}
        ),
    )
    assert data["tools"][0]["parameters"]["required"] == ["path"]


# ---------------------------------------------------------------------------
# Prompt cache: разметка только для claude-моделей (спека prompt-cache §4.2)
# ---------------------------------------------------------------------------

_HISTORY = [
    {"role": "system", "content": "system prompt text"},
    {"role": "user", "content": "hi"},
]


def _strip_prompt_cache(data: dict) -> dict:
    plain = dict(data)
    plain["messages"] = [
        {**m, "content": m["content"][0]["text"]}
        if isinstance(m.get("content"), list)
        and "cache_control" in m["content"][0]
        else m
        for m in plain["messages"]
    ]
    return plain


def _marked_indexes(data: dict) -> list[int]:
    return [
        i
        for i, m in enumerate(data["messages"])
        if isinstance(m.get("content"), list)
        and "cache_control" in m["content"][0]
    ]


def test_create_call_data_completions_marks_claude_model_system_and_tail():
    data = create_call_data_completions(
        _HISTORY, _state(), resolved=_resolved(model="anthropic/claude-opus-5")
    )
    assert "cache_control" not in data
    assert _marked_indexes(data) == [0, 1]
    assert data["messages"][1]["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]
    assert "tool_choice" not in data


def test_create_call_data_completions_leaves_non_claude_payload_untouched():
    data = create_call_data_completions(
        _HISTORY, _state(), resolved=_resolved(model="google/gemini-2.5-flash")
    )
    assert "cache_control" not in json.dumps(data)
    assert all(isinstance(m["content"], str) for m in data["messages"])


def test_create_call_data_completions_claude_payload_equals_plain_after_strip():
    claude = create_call_data_completions(
        _HISTORY, _state(), resolved=_resolved(model="anthropic/claude-opus-5")
    )
    gemini = create_call_data_completions(
        _HISTORY, _state(), resolved=_resolved(model="google/gemini-2.5-flash")
    )
    stripped = _strip_prompt_cache(claude)
    stripped["model"] = "google/gemini-2.5-flash"
    assert stripped == gemini


def test_create_call_data_completions_legacy_path_marks_by_effective_model():
    data = create_call_data_completions(
        _HISTORY, _state(default_model="anthropic/claude-sonnet-5")
    )
    assert _marked_indexes(data) == [0, 1]
    data = create_call_data_completions(
        _HISTORY, _state(default_model="deepseek/deepseek-v4")
    )
    assert _marked_indexes(data) == []


def test_create_call_data_completions_marks_same_for_any_max_tokens():
    first = create_call_data_completions(
        _HISTORY,
        _state(),
        max_tokens=100,
        resolved=_resolved(model="anthropic/claude-opus-5"),
    )
    second = create_call_data_completions(
        _HISTORY,
        _state(),
        max_tokens=50,
        resolved=_resolved(model="anthropic/claude-opus-5"),
    )
    assert first["messages"] == second["messages"]


def test_prompt_cache_per_model_false_disables_marks_for_claude():
    data = create_call_data_completions(
        _HISTORY,
        _state(),
        resolved=_resolved(
            model="anthropic/claude-opus-5",
            backend_overrides={"claude_prompt_cache": False},
        ),
    )
    assert _marked_indexes(data) == []


def test_prompt_cache_per_model_true_enables_marks_for_renamed_model():
    data = create_call_data_completions(
        _HISTORY,
        _state(),
        resolved=_resolved(
            model="sonnet-prod",
            backend_overrides={"claude_prompt_cache": True},
        ),
    )
    assert _marked_indexes(data) == [0, 1]


def test_prompt_cache_global_false_disables_and_per_model_true_overrides():
    off = create_call_data_completions(
        _HISTORY,
        _state(),
        settings=make_settings({"backend.claude_prompt_cache": False}),
        resolved=_resolved(model="anthropic/claude-opus-5"),
    )
    assert _marked_indexes(off) == []
    on = create_call_data_completions(
        _HISTORY,
        _state(),
        settings=make_settings({"backend.claude_prompt_cache": False}),
        resolved=_resolved(
            model="anthropic/claude-opus-5",
            backend_overrides={"claude_prompt_cache": True},
        ),
    )
    assert _marked_indexes(on) == [0, 1]


def test_chat_completion_kwargs_cannot_inject_top_level_cache_control(caplog):
    caplog.set_level(logging.DEBUG, logger="gc_core.utils.connect_backend")
    data = create_call_data_completions(
        _HISTORY,
        _state(),
        settings=make_settings(
            {
                "backend.chat_completion_kwargs": {
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}
                }
            }
        ),
        resolved=_resolved(model="anthropic/claude-opus-5"),
    )
    assert "cache_control" not in data
    assert _marked_indexes(data) == [0, 1]
    assert "cache_control" in caplog.text


def test_chat_completion_kwargs_cache_control_passes_when_marks_are_off():
    """Пока разметка не наша, поле верхнего уровня — рабочая настройка пользователя."""
    kwargs = {
        "backend.chat_completion_kwargs": {
            "cache_control": {"type": "ephemeral"}
        }
    }
    gemini = create_call_data_completions(
        _HISTORY,
        _state(),
        settings=make_settings(kwargs),
        resolved=_resolved(model="google/gemini-2.5-flash"),
    )
    assert gemini["cache_control"] == {"type": "ephemeral"}
    assert _marked_indexes(gemini) == []
    claude_off = create_call_data_completions(
        _HISTORY,
        _state(),
        settings=make_settings(kwargs),
        resolved=_resolved(
            model="anthropic/claude-opus-5",
            backend_overrides={"claude_prompt_cache": False},
        ),
    )
    assert claude_off["cache_control"] == {"type": "ephemeral"}
    assert _marked_indexes(claude_off) == []


def test_service_funnel_never_marks_prompt_cache(monkeypatch):
    """Док обещает: служебные вызовы (судьи, суммаризация) не размечаются —
    даже если консьюмер привязан к claude-модели."""
    captured = _patch_llm_transport(monkeypatch, "ok")
    ctx, router = _router_ctx()
    router.resolve = Mock(
        return_value=_resolved(model="anthropic/claude-opus-5")
    )
    text = run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=ctx,
            state=_state(),
            messages=[
                {"role": "system", "content": "judge"},
                {"role": "user", "content": "x"},
            ],
            max_tokens=50,
            consumer="finish_gate",
        )
    )
    assert text == "ok"
    assert captured[0]["model"] == "anthropic/claude-opus-5"
    assert "cache_control" not in json.dumps(captured[0])
