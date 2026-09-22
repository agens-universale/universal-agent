"""Task 7 (план «Профили v2, волна A»): special_agent на общую chat/completions
воронку + бюджет-хардкоды в конфиг.

Раньше ``SpecialAgent._generate_role`` строил HTTP-вызов вручную и никогда не
прикладывал заголовок ``Authorization`` (латентный баг — генерация роли всегда
уходила без Bearer). Теперь она идёт через
``summarize.llm.arequest_auxiliary_completion_text`` (aux kwargs + strip_think)
— тот же путь, что и любой другой служебный LLM-вызов (summary/finish_gate/
judges/memory_compaction), с заголовками из ``acreate_call_headers``
(auth_connector/resolved-модель).

Заодно бюджет-хардкоды (special_agent role draft, project_memory compaction)
вынесены в конфиг.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from gc_core.config_modules.chat_completions import (
    DEFAULT_SUBAGENT_ROLE_MAX_TOKENS,
    ChatCompletionsLlmConfig,
)
from gc_core.config_modules.project_memory import (
    DEFAULT_PROJECT_MEMORY_COMPACTION_MAX_TOKENS,
    ProjectMemoryConfig,
)
from gc_core.model_router import ResolvedModel
from gc_core.project_memory import compaction
from gc_core.tools.subagents.special_agent import SpecialAgent
from gc_core.usage.account import CATEGORY_SUBAGENT, UsageAccount
from gc_core.usage.context import current_usage_context
from gc_core.utils import llm_chat_client
from gc_testkit import as_coro, run_coro


def _resolved(**overrides) -> ResolvedModel:
    base = dict(
        alias=None,
        model="test-model",
        url="http://alias.local/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )
    base.update(overrides)
    return ResolvedModel(**base)


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        state_id="child-state",
        x_target=None,
        default_model="test-model",
        usage=None,
    )


def _create_instance():
    return asyncio.run(
        SpecialAgent.create_instance(
            role="Security Expert",
            role_description="Web security",
            task="Audit",
            model="test-model",
            config_dict={},
        )
    )


def _ctx(**overrides) -> SimpleNamespace:
    base = dict(
        config_dict={
            "completions_url": "http://backend.local/v1/chat/completions",
            "backend_request_timeout_sec": 600,
            "backend_api_mode": "chat_completions",
        },
        auxiliary_llm=ChatCompletionsLlmConfig(),
        auth_connector=None,
        model_router=None,
        logger=None,
        is_tui=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_default_budgets_match_prior_hardcodes():
    """Schema defaults для бюджетов aux/compaction."""
    assert DEFAULT_SUBAGENT_ROLE_MAX_TOKENS == 24576
    assert ChatCompletionsLlmConfig().subagent_role_max_tokens == 24576
    assert DEFAULT_PROJECT_MEMORY_COMPACTION_MAX_TOKENS == 16_384
    assert ProjectMemoryConfig().compaction_max_tokens == 16_384


def test_special_agent_role_generation_attaches_bearer_header(monkeypatch):
    """Регресс: раньше заголовки собирались вручную из ``chat_completion_headers``
    и Authorization никогда не прикладывался. Теперь ``acreate_call_headers`` (не
    замоканный здесь) читает ``ctx.auth_connector`` и кладёт Bearer-токен."""
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, headers, **kwargs):
        captured["headers"] = headers
        payload = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "generated role"}}]}, payload

    monkeypatch.setattr(llm_chat_client, "arequest_chat_completions_with_policy", as_coro(_fake_policy))

    ctx = _ctx(
        auth_connector=SimpleNamespace(get_token=lambda: "secret-token"),
    )

    instance = _create_instance()
    run_coro(instance.afinalize_role(ctx, _state()))

    assert captured["headers"].get("Authorization") == "Bearer secret-token"
    assert instance.agent_role.startswith("generated role")


def test_special_agent_role_generation_uses_subagent_usage_category(monkeypatch):
    """Регресс ревью Task 7: request_chat_completion_text открывает свой ВНУТРЕННИЙ
    usage_scope из category_for_request_label(request_label) — для
    request_label="subagent_special_agent" он раньше не знал префикса subagent_ и
    дефолтил в CATEGORY_MAIN, перекрывая внешний usage_scope(CATEGORY_SUBAGENT) из
    runner.py на момент фактической записи usage (record_usage вызывается внутри
    транспорта). Проверяем эмпирически: активная категория в момент вызова
    транспорта — subagent."""
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        active = current_usage_context()
        captured["category"] = active.category if active is not None else None
        payload = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "role"}}]}, payload

    monkeypatch.setattr(llm_chat_client, "arequest_chat_completions_with_policy", as_coro(_fake_policy))
    monkeypatch.setattr(llm_chat_client, "acreate_call_headers", as_coro(lambda _s, _c, resolved=None: {}))

    ctx = _ctx()
    state = _state()
    state.usage = UsageAccount()

    instance = _create_instance()
    run_coro(instance.afinalize_role(ctx, state))

    assert captured["category"] == CATEGORY_SUBAGENT


def test_special_agent_role_generation_declares_own_consumer(monkeypatch):
    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        payload = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "role"}}]}, payload

    monkeypatch.setattr(llm_chat_client, "arequest_chat_completions_with_policy", as_coro(_fake_policy))
    monkeypatch.setattr(llm_chat_client, "acreate_call_headers", as_coro(lambda _s, _c, resolved=None: {}))

    router = SimpleNamespace(resolve=Mock(return_value=_resolved()))
    ctx = _ctx(model_router=router)

    instance = _create_instance()
    run_coro(instance.afinalize_role(ctx, _state()))

    router.resolve.assert_called_once_with("subagent:subagent_special_agent")


def test_special_agent_role_generation_uses_configured_max_tokens_default(monkeypatch):
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured["initial_max_tokens"] = initial_max_tokens
        payload = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "role"}}]}, payload

    monkeypatch.setattr(llm_chat_client, "arequest_chat_completions_with_policy", as_coro(_fake_policy))
    monkeypatch.setattr(llm_chat_client, "acreate_call_headers", as_coro(lambda _s, _c, resolved=None: {}))

    ctx = _ctx()

    instance = _create_instance()
    run_coro(instance.afinalize_role(ctx, _state()))

    assert captured["initial_max_tokens"] == DEFAULT_SUBAGENT_ROLE_MAX_TOKENS


def test_special_agent_role_generation_respects_configured_max_tokens_override(monkeypatch):
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured["initial_max_tokens"] = initial_max_tokens
        payload = payload_factory(initial_max_tokens)
        return {"choices": [{"message": {"content": "role"}}]}, payload

    monkeypatch.setattr(llm_chat_client, "arequest_chat_completions_with_policy", as_coro(_fake_policy))
    monkeypatch.setattr(llm_chat_client, "acreate_call_headers", as_coro(lambda _s, _c, resolved=None: {}))

    ctx = _ctx(
        config_dict={
            "completions_url": "http://backend.local/v1/chat/completions",
            "backend_request_timeout_sec": 600,
            "auxiliary_llm_params": {"subagent_role_max_tokens": 222},
        },
        auxiliary_llm=ChatCompletionsLlmConfig(subagent_role_max_tokens=222),
    )

    instance = _create_instance()
    run_coro(instance.afinalize_role(ctx, _state()))

    assert captured["initial_max_tokens"] == 222


def test_project_memory_compaction_uses_configured_max_tokens_default(monkeypatch):
    captured = {}

    def _fake_call(*, max_tokens, **kwargs):
        captured["max_tokens"] = max_tokens
        return "compressed"

    monkeypatch.setattr(
        "gc_core.project_memory.compaction.arequest_auxiliary_completion_text",
        as_coro(_fake_call),
    )

    ctx = SimpleNamespace(
        config_dict={"backend_api_mode": "chat_completions", "model": "test-model"},
        logger=None,
    )
    res = run_coro(compaction.acompact_text("text", ctx))
    assert res.ok is True
    assert captured["max_tokens"] == DEFAULT_PROJECT_MEMORY_COMPACTION_MAX_TOKENS


def test_project_memory_compaction_respects_configured_max_tokens_override(monkeypatch):
    captured = {}

    def _fake_call(*, max_tokens, **kwargs):
        captured["max_tokens"] = max_tokens
        return "compressed"

    monkeypatch.setattr(
        "gc_core.project_memory.compaction.arequest_auxiliary_completion_text",
        as_coro(_fake_call),
    )

    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "model": "test-model",
            "project_memory_params": {"compaction_max_tokens": 111},
        },
        logger=None,
    )
    res = run_coro(compaction.acompact_text("text", ctx))
    assert res.ok is True
    assert captured["max_tokens"] == 111
