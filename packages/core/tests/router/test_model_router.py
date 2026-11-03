from dataclasses import replace

import pytest
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import (
    CONSUMERS,
    SUBAGENT_CONSUMER_PREFIX,
    ModelRouter,
    ResolvedModel,
)


def _snapshot(raw: dict) -> AppConfig:
    """Настоящие провалидированные настройки (``AppConfig``), не мок."""
    return AppConfig.model_validate(raw)


def _router(
    raw: dict,
    *,
    level: str = "default",
    force_legacy: bool = False,
    unbound_override: "ResolvedModel | None" = None,
) -> ModelRouter:
    return ModelRouter(
        _snapshot(raw),
        choice_provider=lambda: level,
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
        "vision",
        "routing_judge",
    )
    assert SUBAGENT_CONSUMER_PREFIX == "subagent:"


def test_consumer_registries_agree():
    """Потребитель заводится во всех реестрах разом. Реестров четыре, и
    пропуск любого молчит: без `_SCALAR_ASSIGN_FIELDS` Router игнорирует
    `assign.<имя>`, без `_ASSIGN_SCALAR_FIELDS` валидатор пропускает опечатку в
    алиасе, без поля схемы конфиг не грузится, без подписи notice отката и строка
    /settings показывают сырое имя."""
    from gc_core.config_modules.model_registry import AssignConfig
    from gc_core.config_modules.model_sections_validation import (
        _ASSIGN_SCALAR_FIELDS,
    )
    from gc_core.model_router import _SCALAR_ASSIGN_FIELDS
    from gc_core.utils.llm_chat_client import CONSUMER_RU_LABEL

    service = set(CONSUMERS) - {"main"}
    assert set(_SCALAR_ASSIGN_FIELDS) == service
    assert set(_ASSIGN_SCALAR_FIELDS) == service
    assert set(AssignConfig.model_fields) - {"subagents"} == service
    assert set(CONSUMER_RU_LABEL) == service


def test_vision_without_binding_follows_the_active_level():
    """«Глаза» без привязки — активная модель, по общему правилу
    (спека изображений §8)."""
    router = _router(
        {
            "models": {"a": {}, "b": {}, "c": {}},
            "slots": {"light": "a", "default": "b", "max": "c"},
        },
        level="max",
    )
    resolved = router.resolve("vision")
    assert resolved.alias == "c"
    assert resolved.source == "активный уровень max"


def test_vision_binding_pins_its_alias():
    router = _router(
        {
            "models": {"glm": {}, "qwen": {}},
            "slots": {"default": "glm"},
            "assign": {"vision": "qwen"},
        }
    )
    resolved = router.resolve("vision")
    assert resolved.alias == "qwen"
    assert resolved.source == "привязка vision→qwen"


def test_vision_in_legacy_is_the_legacy_model():
    """Пустой реестр: `assign` не действует, «глаза» — та же легаси-модель."""
    router = _router(
        {
            "assign": {"vision": "max"},
            "backend": {
                "completions_url": "http://legacy/v1/chat/completions"
            },
            "agent": {"model": "legacy-model"},
        }
    )
    resolved = router.resolve("vision")
    assert resolved.alias is None
    assert resolved.model == "legacy-model"
    assert resolved.source == "легаси"


def test_vision_under_forced_legacy_ignores_the_binding():
    router = _router(
        {
            "models": {"qwen": {}},
            "slots": {"default": "qwen"},
            "assign": {"vision": "qwen"},
            "backend": {"completions_url": "http://legacy/v1"},
            "agent": {"model": "legacy-model"},
        },
        force_legacy=True,
    )
    resolved = router.resolve("vision")
    assert resolved.alias is None
    assert resolved.model == "legacy-model"


def test_vision_in_a_subagent_router_is_the_subagent_model_unless_bound():
    """Дочерний Router субагента: неназначенные «глаза» — модель субагента
    (`unbound_override`), назначенные — своя привязка."""
    raw = {"models": {"flash": {}, "qwen": {}}, "slots": {"default": "flash"}}
    unbound = _router(raw, unbound_override=_pin())
    assert unbound.resolve("vision").alias == "pinned"

    bound = _router(
        {**raw, "assign": {"vision": "qwen"}}, unbound_override=_pin()
    )
    assert bound.resolve("vision").alias == "qwen"


def test_binding_level_resolves_through_slot():
    router = _router(
        {
            "models": {"flash": {"url": "http://flash/v1", "token": ""}},
            "slots": {"light": "flash"},
            "assign": {"finish_gate": "light"},
        }
    )
    resolved = router.resolve("finish_gate")
    assert resolved.alias == "flash"
    assert resolved.model == "flash"
    assert "привязка" in resolved.source and "finish_gate" in resolved.source
    assert "light" in resolved.source


def test_binding_alias_pins_directly_bypassing_level():
    router = _router(
        {
            "models": {
                "deepseek-max": {"url": "http://ds/v1", "token": ""},
                "flash": {"url": "http://fl/v1", "token": ""},
            },
            "slots": {"default": "flash"},
            "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
        },
        level="default",
    )
    resolved = router.resolve("subagent:subagent_planner")
    assert resolved.alias == "deepseek-max"
    assert (
        "привязка" in resolved.source and "subagent_planner" in resolved.source
    )
    assert "deepseek-max" in resolved.source


def test_subagent_exact_binding_beats_wildcard():
    router = _router(
        {
            "models": {"deepseek-max": {}, "flash": {}},
            "slots": {"light": "flash"},
            "assign": {
                "subagents": {"subagent_planner": "deepseek-max", "*": "light"}
            },
        }
    )
    assert router.resolve("subagent:subagent_planner").alias == "deepseek-max"
    assert router.resolve("subagent:subagent_critic").alias == "flash"


def test_subagent_no_binding_falls_to_active_level():
    router = _router(
        {
            "models": {"flash": {}},
            "slots": {"max": "flash"},
            "assign": {},
        },
        level="max",
    )
    resolved = router.resolve("subagent:subagent_worker")
    assert resolved.alias == "flash"
    assert "активный" in resolved.source


def test_unbound_scalar_consumer_uses_active_level():
    router = _router(
        {
            "models": {"glm-local": {}},
            "slots": {"max": "glm-local"},
        },
        level="max",
    )
    # PR140 Р1: finish_gate теперь с фиксированным уровнем, активный уровень
    # проверяем на потребителе вне таблицы unbound_level.
    resolved = router.resolve("intent_scope")
    assert resolved.alias == "glm-local"
    assert "активный" in resolved.source and "max" in resolved.source


def test_level_fallback_matrix_when_requested_slot_unset():
    router = _router(
        {
            "models": {"b": {}},
            "slots": {"default": "b"},  # light не задан
        },
        level="light",
    )
    resolved = router.resolve("main")
    assert resolved.alias == "b"
    assert "фоллбэк" in resolved.source
    assert "light" in resolved.source and "default" in resolved.source


def test_raw_auto_level_raises_honest_error_instead_of_key_error():
    """`_resolve_level` индексирует
    `LEVEL_FALLBACK[level]` — сырой литерал `auto` ("`level: auto`" не переведён
    через `auto_routing.levels.effective_target`) раньше падал `KeyError: 'auto'`.
    Честная ошибка вместо этого — молчаливого дефолта нет."""
    router = _router(
        {"models": {"b": {}}, "slots": {"default": "b"}}, level="auto"
    )
    with pytest.raises(ValueError) as excinfo:
        router.resolve_level("auto")
    message = str(excinfo.value)
    assert "auto" in message
    assert "effective_target" in message


def test_single_model_registry_without_any_slot_resolves_to_it():
    router = _router(
        {"models": {"solo": {"url": "http://solo/v1", "token": ""}}},
        level="light",
    )
    resolved = router.resolve("main")
    assert resolved.alias == "solo"
    assert resolved.source  # заполнен


def test_legacy_when_merged_registry_empty():
    router = _router(
        {
            "backend": {
                "completions_url": "http://legacy/v1/chat/completions",
            },
            "agent": {"model": "legacy-model"},
        }
    )
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
    router = _router(
        {
            "assign": {
                "finish_gate": "light",
                "summary": "max",
                "subagents": {"subagent_critic": "max", "*": "light"},
            },
            "backend": {
                "completions_url": "http://legacy/v1/chat/completions"
            },
            "agent": {"model": "legacy-model"},
        }
    )
    assert router.registry_empty() is True

    for consumer in (
        "main",
        "finish_gate",
        "summary",
        "subagent:subagent_critic",
        "subagent:subagent_worker",
    ):
        resolved = router.resolve(consumer)
        assert resolved.alias is None, (
            f"{consumer}: привязка сработала без реестра"
        )
        assert resolved.model == "legacy-model"
        assert resolved.url == "http://legacy/v1/chat/completions"
        assert resolved.source == "легаси"


def test_legacy_force_overrides_nonempty_registry():
    router = _router(
        {
            "models": {"flash": {"url": "http://flash/v1", "token": ""}},
            "slots": {"default": "flash"},
            "backend": {"completions_url": "http://legacy/v1"},
            "agent": {"model": "legacy-model"},
        },
        force_legacy=True,
    )
    resolved = router.resolve("main")
    assert resolved.alias is None
    assert resolved.model == "legacy-model"
    assert "легаси-форс" in resolved.source
    assert router.registry_empty() is False


def test_model_inherits_url_and_token_from_effective_backend_when_unset():
    router = _router(
        {
            "models": {"bare": {}},
            "slots": {"default": "bare"},
            "backend": {
                "completions_url": "http://global/v1/chat/completions",
                "chat_completion_headers": {
                    "Authorization": "Bearer global-token"
                },
            },
        }
    )
    resolved = router.resolve("main")
    assert resolved.model == "bare"  # model: опущен → id = алиас
    assert resolved.url == "http://global/v1/chat/completions"
    assert (
        resolved.authorization is None
    )  # None = наследовать эффективный заголовок


def test_model_token_preserves_configured_authorization_format():
    router = _router(
        {
            "models": {
                "secure": {"url": "http://secure/v1", "token": "sk-abc"}
            },
            "slots": {"default": "secure"},
        }
    )
    assert router.resolve("main").authorization == "sk-abc"


def test_model_token_empty_string_means_explicitly_no_auth():
    router = _router(
        {
            "models": {"open": {"url": "http://open/v1", "token": ""}},
            "slots": {"default": "open"},
        }
    )
    assert router.resolve("main").authorization == ""


def test_model_url_inherited_from_own_backend_completions_url_override():
    router = _router(
        {
            "models": {
                "viaBackend": {
                    "token": "",
                    "backend": {"completions_url": "http://backend-url/v1"},
                }
            },
            "slots": {"default": "viaBackend"},
        }
    )
    resolved = router.resolve("main")
    assert resolved.url == "http://backend-url/v1"
    assert (
        resolved.backend_overrides.get("completions_url")
        == "http://backend-url/v1"
    )


def test_backend_overrides_only_explicitly_set_keys():
    # chat_completion_kwargs на уровне алиаса схема отвергает (MODEL_LEVEL_UNSUPPORTED,
    # kwargs алиаса — в `main`/`auxiliary`), поэтому явный ключ — таймаут.
    router = _router(
        {
            "models": {"flash": {"backend": {"request_timeout_sec": 42}}},
            "slots": {"default": "flash"},
        }
    )
    resolved = router.resolve("main")
    assert resolved.backend_overrides == {"request_timeout_sec": 42}


def test_tariff_passed_through_from_model_pricing():
    router = _router(
        {
            "models": {"flash": {"pricing": {"input": 12, "output": 48}}},
            "slots": {"default": "flash"},
        }
    )
    tariff = router.resolve("main").tariff
    assert tariff["input"] == 12.0
    assert tariff["output"] == 48.0


def test_tariff_none_when_model_has_no_pricing():
    router = _router({"models": {"flash": {}}, "slots": {"default": "flash"}})
    assert router.resolve("main").tariff is None


def test_active_level_reflects_choice_provider_regardless_of_binding():
    router = _router(
        {"models": {"a": {}}, "slots": {"default": "a"}}, level="max"
    )
    assert router.active_target() == "max"


def test_registry_empty_false_when_models_present():
    router = _router({"models": {"a": {}}, "slots": {"default": "a"}})
    assert router.registry_empty() is False


def test_unknown_consumer_raises_value_error():
    router = _router({"models": {"a": {}}, "slots": {"default": "a"}})
    with pytest.raises(ValueError):
        router.resolve("not_a_real_consumer")


@pytest.mark.parametrize(
    "consumer", list(CONSUMERS) + ["subagent:subagent_worker"]
)
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
    assert router.resolve("intent_scope").alias == "pinned"


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


def test_command_judge_without_binding_uses_default_not_the_active_level():
    """Судья команд (edit) и действий (auto) без привязки — уровнем default:
    как классификатор у эталона, не моделью, которую человек выбрал для хода.
    На max каждая проверка иначе шла бы в самую тяжёлую модель."""
    router = _router(
        {
            "models": {"a": {}, "b": {}, "c": {}},
            "slots": {"light": "a", "default": "b", "max": "c"},
        },
        level="max",
    )
    resolved = router.resolve("command_judge")
    assert resolved.alias == "b"
    assert "command_judge" in resolved.source and "default" in resolved.source


def test_same_call_kit_ignores_only_the_source():
    """Комплект вызова — всё, кроме витринного `source`: фоллбэк незаданного слота
    и сам слот дают тот же запрос; одна модель с разными параметрами — нет."""
    router = _router(
        {
            "models": {
                "cold": {
                    "model": "m-1",
                    "main": {"temperature": 0.2},
                },
                "hot": {
                    "model": "m-1",
                    "main": {"temperature": 0.9},
                },
            },
            "slots": {"light": "cold", "default": "hot"},
        }
    )
    default = router.resolve_level("default")
    fallback = router.resolve_level("max")  # max не задан → фоллбэк на default
    assert (
        fallback.source != default.source
        and fallback.same_call_kit(default) is True
    )
    assert router.resolve_level("light").same_call_kit(default) is False


def test_same_call_kit_compares_every_field_but_the_source():
    """Один алиас, различие в одном поле: параметры, адрес и токен различают
    комплекты вызова, витринное `source` — нет."""
    kit = ResolvedModel(
        alias="mid",
        model="m-1",
        url="http://127.0.0.1:8014/v1/chat/completions",
        authorization=None,
        backend_overrides={"temperature": 0.2},
        tariff=None,
        source="уровень default",
    )
    assert (
        kit.same_call_kit(replace(kit, backend_overrides={"temperature": 0.9}))
        is False
    )
    assert (
        kit.same_call_kit(
            replace(kit, url="http://127.0.0.1:8015/v1/chat/completions")
        )
        is False
    )
    assert (
        kit.same_call_kit(replace(kit, authorization="Bearer other")) is False
    )
    assert (
        kit.same_call_kit(replace(kit, source="фоллбэк max → default")) is True
    )


# --- модель main-трейса для заданного уровня вызова ---------------------------

_TWO_LEVELS = {
    "models": {
        "small": {"url": "http://small/v1/chat/completions", "token": ""},
        "big": {"url": "http://big/v1/chat/completions", "token": ""},
    },
    "slots": {"light": "small", "default": "big"},
}


def test_resolve_main_at_follows_the_given_level_not_the_live_one():
    """Уровень вызова задаёт вызывающий (сегодня — закреплённый на ход): живой
    `/level`, переключённый посреди хода, резолв не уводит."""
    router = _router(_TWO_LEVELS, level="default")
    assert router.resolve_main_at("light").alias == "small"
    assert router.resolve("main").alias == "big"


def test_resolve_main_at_without_a_level_is_the_live_resolve():
    router = _router(_TWO_LEVELS, level="default")
    assert router.resolve_main_at(None).alias == "big"


def test_resolve_main_at_keeps_the_subagent_override():
    """У дочернего Router'а модель заморожена в нём самом — уровень родителя её
    не подменяет."""
    parent = _router(_TWO_LEVELS, level="default")
    child = parent.with_override(parent.resolve_level("light"))
    assert child.resolve_main_at("default").alias == "small"


def test_resolve_level_can_skip_the_breaker_for_showcase_reads():
    """Порог сжатия — свойство записи модели: открытый предохранитель не должен
    ронять его чтение. Рантайм-резолв по умолчанию отказывает, как и раньше."""
    from gc_core.model_router import AliasCircuitOpenError

    router = _router(_TWO_LEVELS, level="default")
    router.breaker.trip("small", "5xx после ретраев")
    with pytest.raises(AliasCircuitOpenError):
        router.resolve_level("light")
    with pytest.raises(AliasCircuitOpenError):
        router.resolve_main_at("light")
    assert router.resolve_level("light", check_breaker=False).alias == "small"
    assert (
        router.resolve_main_at("light", check_breaker=False).alias == "small"
    )


def _alias_settings(slots: dict) -> AppConfig:
    models = {
        alias: {"url": f"http://{alias}/v1/chat/completions", "token": ""}
        for alias in ("a", "b")
    }
    return _snapshot({"models": models, "slots": slots})


def test_choice_alias_resolves_main_to_that_model():
    router = ModelRouter(
        _alias_settings({"default": "a"}), choice_provider=lambda: "b"
    )
    assert router.resolve("main").alias == "b"
    assert router.active_target() == "b"


def test_level_word_choice_still_resolves_its_slot():
    router = ModelRouter(
        _alias_settings({"max": "b"}), choice_provider=lambda: "max"
    )
    assert router.resolve("main").alias == "b"


def test_resolve_main_at_alias_target_even_when_auto_allowed():
    router = ModelRouter(
        _alias_settings({"default": "a"}), choice_provider=lambda: "auto"
    )
    assert router.resolve_main_at("b").alias == "b"


def test_router_take_probe_none_and_legacy_resolve_are_false():
    router = _router({})
    assert router.take_probe(None) is False
    legacy = replace(_pin(), alias=None)
    assert router.take_probe(legacy) is False


def test_router_take_probe_is_one_shot_for_allowed_alias():
    router = _router({})
    router.breaker.trip("pinned", "ReadError")
    router.breaker.allow_probe()
    assert router.take_probe(_pin()) is True
    assert router.take_probe(_pin()) is False
