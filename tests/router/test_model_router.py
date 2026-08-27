import pytest

from config import flatten_app_config
from config_modules.root import AppConfig
from model_router import CONSUMERS, ModelRouter, ResolvedModel, SUBAGENT_CONSUMER_PREFIX


def _snapshot(raw: dict) -> dict:
    """Реальный снимок config_dict — через настоящий flatten_app_config, не мок."""
    app_cfg = AppConfig.model_validate(raw)
    return flatten_app_config(app_cfg)


def _router(
    raw: dict,
    *,
    level: str = "default",
    force_legacy: bool = False,
    unbound_override: "ResolvedModel | None" = None,
) -> ModelRouter:
    return ModelRouter(
        _snapshot(raw),
        level_provider=lambda: level,
        force_legacy=force_legacy,
        unbound_override=unbound_override,
    )


def test_consumers_constant_matches_spec():
    assert CONSUMERS == (
        "main",
        "finish_gate",
        "text_answer_judge",
        "command_judge",
        "summary",
        "intent_scope",
        "memory_compaction",
        "auto_judge",
    )
    assert SUBAGENT_CONSUMER_PREFIX == "subagent:"


def test_binding_level_resolves_through_slot():
    router = _router({
        "models": {"flash": {"url": "http://flash/v1"}},
        "slots": {"light": "flash"},
        "assign": {"finish_gate": "light"},
    })
    resolved = router.resolve("finish_gate")
    assert resolved.alias == "flash"
    assert resolved.model == "flash"
    assert "привязка" in resolved.source and "finish_gate" in resolved.source
    assert "light" in resolved.source


def test_binding_alias_pins_directly_bypassing_level():
    router = _router({
        "models": {"deepseek-max": {"url": "http://ds/v1"}, "flash": {"url": "http://fl/v1"}},
        "slots": {"default": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    }, level="default")
    resolved = router.resolve("subagent:subagent_planner")
    assert resolved.alias == "deepseek-max"
    assert "привязка" in resolved.source and "subagent_planner" in resolved.source
    assert "deepseek-max" in resolved.source


def test_subagent_exact_binding_beats_wildcard():
    router = _router({
        "models": {"deepseek-max": {}, "flash": {}},
        "slots": {"light": "flash"},
        "assign": {"subagents": {"subagent_planner": "deepseek-max", "*": "light"}},
    })
    assert router.resolve("subagent:subagent_planner").alias == "deepseek-max"
    assert router.resolve("subagent:subagent_critic").alias == "flash"


def test_subagent_no_binding_falls_to_active_level():
    router = _router({
        "models": {"flash": {}},
        "slots": {"max": "flash"},
        "assign": {},
    }, level="max")
    resolved = router.resolve("subagent:subagent_worker")
    assert resolved.alias == "flash"
    assert "активный" in resolved.source


def test_unbound_scalar_consumer_uses_active_level():
    router = _router({
        "models": {"glm-local": {}},
        "slots": {"max": "glm-local"},
    }, level="max")
    resolved = router.resolve("finish_gate")
    assert resolved.alias == "glm-local"
    assert "активный" in resolved.source and "max" in resolved.source


def test_auto_judge_without_binding_uses_default_statically_not_active_level():
    router = _router({
        "models": {"a": {}, "b": {}, "c": {}},
        "slots": {"light": "a", "default": "b", "max": "c"},
    }, level="max")  # активный уровень max — auto_judge обязан игнорировать это
    resolved = router.resolve("auto_judge")
    assert resolved.alias == "b"
    assert "default" in resolved.source
    assert "auto_judge" in resolved.source


def test_level_fallback_matrix_when_requested_slot_unset():
    router = _router({
        "models": {"b": {}},
        "slots": {"default": "b"},  # light не задан
    }, level="light")
    resolved = router.resolve("main")
    assert resolved.alias == "b"
    assert "фоллбэк" in resolved.source
    assert "light" in resolved.source and "default" in resolved.source


def test_single_model_registry_without_any_slot_resolves_to_it():
    router = _router({"models": {"solo": {"url": "http://solo/v1"}}}, level="light")
    resolved = router.resolve("main")
    assert resolved.alias == "solo"
    assert resolved.source  # заполнен


def test_legacy_when_merged_registry_empty():
    router = _router({
        "backend": {
            "completions_url": "http://legacy/v1/chat/completions",
        },
        "agent": {"model": "legacy-model"},
    })
    resolved = router.resolve("main")
    assert resolved.alias is None
    assert resolved.model == "legacy-model"
    assert resolved.url == "http://legacy/v1/chat/completions"
    assert resolved.authorization is None
    assert resolved.source == "легаси"
    assert router.registry_empty() is True


def test_assign_bindings_are_inert_while_registry_is_empty():
    """§14: «assign уровнями при пустом реестре — валиден, инертен + warning».

    Схема и warning закрыты в `tests/config/test_model_sections_phase2.py`
    (`test_empty_registry_assign_level_valid_and_warns`), а ИНЕРТНОСТЬ — только
    здесь: конфиг с привязками, но без реестра, обязан резолвиться легаси
    ЦЕЛИКОМ. Иначе привязка `finish_gate: light` увела бы судью на
    несуществующий слот — ровно тот случай, ради которого warning и написан.
    """
    router = _router({
        "assign": {
            "finish_gate": "light",
            "summary": "max",
            "subagents": {"subagent_critic": "max", "*": "light"},
        },
        "backend": {"completions_url": "http://legacy/v1/chat/completions"},
        "agent": {"model": "legacy-model"},
    })
    assert router.registry_empty() is True

    for consumer in ("main", "finish_gate", "summary",
                     "subagent:subagent_critic", "subagent:subagent_worker"):
        resolved = router.resolve(consumer)
        assert resolved.alias is None, f"{consumer}: привязка сработала без реестра"
        assert resolved.model == "legacy-model"
        assert resolved.url == "http://legacy/v1/chat/completions"
        assert resolved.source == "легаси"


def test_legacy_force_overrides_nonempty_registry():
    router = _router({
        "models": {"flash": {"url": "http://flash/v1"}},
        "slots": {"default": "flash"},
        "backend": {"completions_url": "http://legacy/v1"},
        "agent": {"model": "legacy-model"},
    }, force_legacy=True)
    resolved = router.resolve("main")
    assert resolved.alias is None
    assert resolved.model == "legacy-model"
    assert "легаси-форс" in resolved.source
    assert router.registry_empty() is False


def test_model_inherits_url_and_token_from_effective_backend_when_unset():
    router = _router({
        "models": {"bare": {}},
        "slots": {"default": "bare"},
        "backend": {
            "completions_url": "http://global/v1/chat/completions",
            "chat_completion_headers": {"Authorization": "Bearer global-token"},
        },
    })
    resolved = router.resolve("main")
    assert resolved.model == "bare"  # model: опущен → id = алиас
    assert resolved.url == "http://global/v1/chat/completions"
    assert resolved.authorization is None  # None = наследовать эффективный заголовок


def test_model_token_normalized_to_bearer():
    router = _router({
        "models": {"secure": {"token": "sk-abc"}},
        "slots": {"default": "secure"},
    })
    assert router.resolve("main").authorization == "Bearer sk-abc"


def test_model_token_empty_string_means_explicitly_no_auth():
    router = _router({
        "models": {"open": {"token": ""}},
        "slots": {"default": "open"},
    })
    assert router.resolve("main").authorization == ""


def test_model_url_inherited_from_own_backend_completions_url_override():
    router = _router({
        "models": {"viaBackend": {"backend": {"completions_url": "http://backend-url/v1"}}},
        "slots": {"default": "viaBackend"},
    })
    resolved = router.resolve("main")
    assert resolved.url == "http://backend-url/v1"
    assert resolved.backend_overrides.get("completions_url") == "http://backend-url/v1"


def test_backend_overrides_only_explicitly_set_keys():
    router = _router({
        "models": {"flash": {"backend": {"chat_completion_kwargs": {"temperature": 0.2}}}},
        "slots": {"default": "flash"},
    })
    resolved = router.resolve("main")
    assert resolved.backend_overrides == {"chat_completion_kwargs": {"temperature": 0.2}}


def test_tariff_passed_through_from_model_pricing():
    router = _router({
        "models": {"flash": {"pricing": {"input": 12, "output": 48}}},
        "slots": {"default": "flash"},
    })
    tariff = router.resolve("main").tariff
    assert tariff["input"] == 12.0
    assert tariff["output"] == 48.0


def test_tariff_none_when_model_has_no_pricing():
    router = _router({"models": {"flash": {}}, "slots": {"default": "flash"}})
    assert router.resolve("main").tariff is None


def test_active_level_reflects_level_provider_regardless_of_binding():
    router = _router({"models": {"a": {}}, "slots": {"default": "a"}}, level="max")
    assert router.active_level() == "max"


def test_registry_empty_false_when_models_present():
    router = _router({"models": {"a": {}}, "slots": {"default": "a"}})
    assert router.registry_empty() is False


def test_unknown_consumer_raises_value_error():
    router = _router({"models": {"a": {}}, "slots": {"default": "a"}})
    with pytest.raises(ValueError):
        router.resolve("not_a_real_consumer")


@pytest.mark.parametrize("consumer", list(CONSUMERS) + ["subagent:subagent_worker"])
def test_source_always_filled_for_every_consumer(consumer):
    router = _router({"models": {"a": {}}, "slots": {"default": "a"}})
    resolved = router.resolve(consumer)
    assert isinstance(resolved.source, str) and resolved.source.strip()


# --- unbound_override (спека §7 п.4, дочерний Router субагента) ------------------


def _pin(alias: str = "pinned") -> ResolvedModel:
    return ResolvedModel(
        alias=alias,
        model=f"{alias}-model",
        url=f"http://{alias}.local/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test-pin",
    )


def test_unbound_override_used_for_main_without_binding():
    router = _router(
        {"models": {"flash": {}}, "slots": {"default": "flash"}},
        level="default",
        unbound_override=_pin(),
    )
    assert router.resolve("main").alias == "pinned"


def test_unbound_override_used_for_unbound_scalar_consumer():
    router = _router(
        {"models": {"flash": {}}, "slots": {"default": "flash"}},
        level="default",
        unbound_override=_pin(),
    )
    assert router.resolve("summary").alias == "pinned"


def test_unbound_override_ignored_when_binding_is_level():
    """Регресс ревью Task 8: явная привязка К УРОВНЮ — «более точная привязка»
    (спека §7 п.4/§5.2, индирекция через слоты) — не должна перекрываться
    override'ом непривязанных потребителей."""
    router = _router(
        {
            "models": {"flash": {}},
            "slots": {"default": "flash"},
            "assign": {"summary": "default"},
        },
        level="default",
        unbound_override=_pin(),
    )
    resolved = router.resolve("summary")
    assert resolved.alias == "flash"
    assert "привязка" in resolved.source


def test_unbound_override_ignored_when_binding_is_alias():
    router = _router(
        {
            "models": {"flash": {}, "glm-flash": {}},
            "slots": {"default": "flash"},
            "assign": {"finish_gate": "glm-flash"},
        },
        level="default",
        unbound_override=_pin(),
    )
    resolved = router.resolve("finish_gate")
    assert resolved.alias == "glm-flash"


def test_unbound_override_none_by_default_keeps_level_path():
    router = _router({"models": {"flash": {}}, "slots": {"default": "flash"}})
    assert router.resolve("main").alias == "flash"
