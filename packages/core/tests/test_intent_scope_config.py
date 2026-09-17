from gc_core.config_modules.intent_scope import DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT, DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS, IntentScopeConfig, IntentScopeToolConfig, backend_allows_intent_scope_hook, intent_scope_active, intent_scope_from_config_dict, intent_scope_tool_record, normalize_intent_scope_config


def test_intent_scope_active_requires_non_empty_tools():
    assert not intent_scope_active(IntentScopeConfig(tools={}))
    assert intent_scope_active(
        IntentScopeConfig(tools={"turn_ask_human": IntentScopeToolConfig(record="aggregated")})
    )


def test_intent_scope_from_config_dict_prefers_model_on_dict():
    model = IntentScopeConfig(
        tools={"turn_finish": IntentScopeToolConfig(record="raw")}
    )
    cfg = intent_scope_from_config_dict(
        {
            "intent_scope": model,
            "intent_scope_params": {"tools": {"turn_ask_human": {"record": "aggregated"}}},
        }
    )
    assert cfg is model
    assert cfg.tools["turn_finish"].record == "raw"


def test_intent_scope_from_config_dict_from_params():
    cfg = intent_scope_from_config_dict(
        {
            "intent_scope_params": {
                "tools": {"turn_ask_human": {"record": "aggregated"}},
                "max_summary_chars": 400,
            }
        }
    )
    assert cfg.tools["turn_ask_human"].record == "aggregated"
    assert cfg.max_summary_chars == 400


def test_intent_scope_from_config_dict_defaults():
    cfg = intent_scope_from_config_dict({})
    assert cfg.tools == {}
    assert cfg.fallback_raw_on_llm_error is True
    assert cfg.read_page_record_limit == DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT
    assert cfg.max_raw_record_chars == DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS


def test_intent_scope_read_page_record_limit_from_config():
    cfg = intent_scope_from_config_dict(
        {"intent_scope_params": {"read_page_record_limit": 25}}
    )
    assert cfg.read_page_record_limit == 25


def test_normalize_intent_scope_config_clamps_invalid_read_page_record_limit():
    cfg = IntentScopeConfig(read_page_record_limit=0)
    normalize_intent_scope_config(cfg)
    assert cfg.read_page_record_limit == DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT


def test_normalize_intent_scope_config_clamps_invalid_max_raw_record_chars():
    cfg = IntentScopeConfig(max_raw_record_chars=0)
    normalize_intent_scope_config(cfg)
    assert cfg.max_raw_record_chars == DEFAULT_INTENT_SCOPE_MAX_RAW_RECORD_CHARS


def test_backend_allows_intent_scope_hook_only_chat_completions():
    assert not backend_allows_intent_scope_hook({"backend_api_mode": "responses"})
    assert backend_allows_intent_scope_hook({"backend_api_mode": "chat_completions"})


def test_intent_scope_tools_map_accepts_record_modes():
    cfg = IntentScopeConfig.model_validate(
        {
            "tools": {
                "turn_ask_human": {"record": "aggregated"},
                "turn_finish": {"record": "raw"},
            }
        }
    )
    assert intent_scope_tool_record(cfg, "turn_ask_human") == "aggregated"
    assert intent_scope_tool_record(cfg, "turn_finish") == "raw"
    assert intent_scope_tool_record(cfg, "missing") is None
