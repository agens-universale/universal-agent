from gc_core.config_modules.intent_scope import (
    DEFAULT_INTENT_SCOPE_MAX_FINAL_ANSWER_RAW_CHARS,
    IntentScopeConfig,
    normalize_intent_scope_config,
)


def test_default_final_answer_threshold_is_2000():
    cfg = IntentScopeConfig()
    assert cfg.max_final_answer_raw_chars == 2000
    assert DEFAULT_INTENT_SCOPE_MAX_FINAL_ANSWER_RAW_CHARS == 2000


def test_final_answer_summary_prompt_path_default_none():
    assert (
        IntentScopeConfig().intent_scope_final_answer_summary_prompt_path
        is None
    )


def test_normalize_fixes_nonpositive_threshold():
    cfg = IntentScopeConfig(max_final_answer_raw_chars=0)
    cfg = normalize_intent_scope_config(cfg)
    assert (
        cfg.max_final_answer_raw_chars
        == DEFAULT_INTENT_SCOPE_MAX_FINAL_ANSWER_RAW_CHARS
    )
