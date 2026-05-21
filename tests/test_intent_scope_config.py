from config_modules.intent_scope import (
    IntentScopeConfig,
    backend_allows_intent_scope_hook,
    intent_scope_active,
    intent_scope_from_config_dict,
)


def test_intent_scope_active_requires_non_empty_tools():
    assert not intent_scope_active(IntentScopeConfig(tools=[]))
    assert intent_scope_active(IntentScopeConfig(tools=["ask_human"]))


def test_intent_scope_from_config_dict_prefers_model_on_dict():
    model = IntentScopeConfig(tools=["submit_solution"])
    cfg = intent_scope_from_config_dict(
        {"intent_scope": model, "intent_scope_params": {"tools": ["ask_human"]}}
    )
    assert cfg is model
    assert cfg.tools == ["submit_solution"]


def test_intent_scope_from_config_dict_from_params():
    cfg = intent_scope_from_config_dict(
        {"intent_scope_params": {"tools": ["ask_human"], "max_summary_chars": 400}}
    )
    assert cfg.tools == ["ask_human"]
    assert cfg.max_summary_chars == 400


def test_intent_scope_from_config_dict_defaults():
    cfg = intent_scope_from_config_dict({})
    assert cfg.tools == []


def test_backend_allows_intent_scope_hook_only_chat_completions():
    assert not backend_allows_intent_scope_hook({"backend_api_mode": "responses"})
    assert backend_allows_intent_scope_hook({"backend_api_mode": "chat_completions"})
