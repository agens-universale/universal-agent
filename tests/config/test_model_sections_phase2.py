import pytest

from config_modules.model_registry import LEVEL_FALLBACK
from config_modules.model_sections_validation import validate_model_sections
from config_modules.root import AppConfig


def test_slot_to_missing_alias_errors_with_coordinate():
    cfg = AppConfig.model_validate({"models": {"a": {}}, "slots": {"default": "ghost"}})
    with pytest.raises(Exception, match=r"slots\.default.*ghost"):
        validate_model_sections(cfg)


def test_default_resolvability_two_models_no_slots():
    cfg = AppConfig.model_validate({"models": {"a": {}, "b": {}}})
    with pytest.raises(Exception, match="slots.default"):
        validate_model_sections(cfg)


def test_one_model_no_slots_ok():
    assert validate_model_sections(AppConfig.model_validate({"models": {"a": {}}})) == []


def test_cross_layer_reference_checked_on_merged_only():
    # слот в config.yml на алиас из профиля: фаза 1 не проверяет ссылки — проверка только тут
    assert validate_model_sections(AppConfig.model_validate(
        {"models": {"from-profile": {}}, "slots": {"light": "from-profile"}})) == []


def test_three_models_one_slot_set_default_resolves_via_matrix():
    # хотя бы один слот задан → default резолвится матрицей фоллбэков, ошибки нет
    cfg = AppConfig.model_validate({"models": {"a": {}, "b": {}, "c": {}}, "slots": {"light": "a"}})
    assert validate_model_sections(cfg) == []


def test_one_model_with_slot_ok():
    cfg = AppConfig.model_validate({"models": {"x": {}}, "slots": {"light": "x"}})
    assert validate_model_sections(cfg) == []


def test_assign_value_level_ok():
    cfg = AppConfig.model_validate({"models": {"a": {}}, "assign": {"finish_gate": "light"}})
    assert validate_model_sections(cfg) == []


def test_assign_value_alias_ok():
    cfg = AppConfig.model_validate({"models": {"flash": {}}, "assign": {"finish_gate": "flash"}})
    assert validate_model_sections(cfg) == []


def test_assign_value_unknown_errors_with_coordinate():
    cfg = AppConfig.model_validate({"models": {"a": {}}, "assign": {"finish_gate": "ghost"}})
    with pytest.raises(Exception, match=r"assign\.finish_gate.*ghost"):
        validate_model_sections(cfg)


def test_assign_value_auto_forbidden():
    cfg = AppConfig.model_validate({"models": {"a": {}}, "assign": {"summary": "auto"}})
    with pytest.raises(Exception, match=r"assign\.summary.*auto"):
        validate_model_sections(cfg)


def test_assign_subagents_known_name_ok():
    cfg = AppConfig.model_validate({
        "models": {"deepseek-max": {}},
        "assign": {"subagents": {"subagent_planner": "deepseek-max"}},
    })
    assert validate_model_sections(cfg) == []


def test_assign_subagents_wildcard_ok():
    cfg = AppConfig.model_validate({
        "models": {"a": {}},
        "assign": {"subagents": {"*": "light"}},
    })
    assert validate_model_sections(cfg) == []


def test_assign_subagents_unknown_name_errors_with_coordinate():
    cfg = AppConfig.model_validate({
        "models": {"a": {}},
        "assign": {"subagents": {"subagent_bogus": "light"}},
    })
    with pytest.raises(Exception, match=r"assign\.subagents\.subagent_bogus"):
        validate_model_sections(cfg)


def test_assign_subagents_value_auto_forbidden():
    cfg = AppConfig.model_validate({
        "models": {"a": {}},
        "assign": {"subagents": {"subagent_planner": "auto"}},
    })
    with pytest.raises(Exception, match="auto"):
        validate_model_sections(cfg)


def test_empty_registry_slots_with_alias_errors():
    cfg = AppConfig.model_validate({"slots": {"default": "glm"}})
    with pytest.raises(Exception, match=r"slots\.default.*glm"):
        validate_model_sections(cfg)


def test_empty_registry_assign_level_valid_and_warns():
    cfg = AppConfig.model_validate({"assign": {"finish_gate": "light"}})
    warnings = validate_model_sections(cfg)
    assert any("привязки без реестра моделей не действуют" in w for w in warnings)


def test_empty_registry_untouched_no_warning():
    cfg = AppConfig.model_validate({})
    assert validate_model_sections(cfg) == []


def test_source_hint_prefixes_error():
    cfg = AppConfig.model_validate({"models": {"a": {}}, "slots": {"default": "ghost"}})
    with pytest.raises(Exception, match=r"~/.gc/profiles/work\.yml.*slots\.default"):
        validate_model_sections(cfg, source_hint="~/.gc/profiles/work.yml")


# --- §7: ослабленный транспорт под общим токеном ------------------------------

_WEAKENED = "запись gc ослабляет защиту запроса, который идёт с общим токеном"


def test_inherited_pair_with_ssl_off_warns_about_the_shared_token():
    """§7 (решение владельца 2): `use_ssl`/сертификаты в правило пары НЕ входят — они
    не меняют адрес, а ослабляют защиту запроса. Но запрос этот идёт с ОБЩИМ токеном
    (пара унаследована), и уходит он по ослабленному каналу — про это предупреждаем при
    старте. Notice, а не отказ: сетап с самоподписанным сертификатом законен."""
    cfg = AppConfig.model_validate({
        "models": {"gc": {"model": "x", "backend": {"use_ssl": False}}},
    })
    assert validate_model_sections(cfg) == [_WEAKENED]


def test_inherited_pair_with_client_cert_warns_too():
    """Клиентский сертификат меняет тот же запрос с общим токеном — и предупреждение
    у него то же: человек обязан увидеть, ЧЕЙ токен уходит по изменённому каналу."""
    for backend in ({"cert_path": "/c.crt"}, {"key_path": "/k.key"}):
        cfg = AppConfig.model_validate({"models": {"gc": {"model": "x", "backend": backend}}})
        assert validate_model_sections(cfg) == [_WEAKENED], backend


def test_own_pair_with_ssl_off_is_not_warned_about():
    """Своя пара — свой токен: ослабленный канал уносит СВОЙ ключ записи на СВОЙ адрес,
    и предупреждать про общий токен здесь не о чем (он туда не идёт, §2)."""
    cfg = AppConfig.model_validate({
        "models": {"gc": {"url": "http://own/v1", "token": "sk-own",
                          "backend": {"use_ssl": False}}},
    })
    assert validate_model_sections(cfg) == []


def test_inherited_pair_without_transport_quirks_is_silent():
    """Обратная половина: унаследованная пара сама по себе — штатная форма записи, и
    notice на каждой такой строке был бы шумом."""
    cfg = AppConfig.model_validate({"models": {"gc": {"model": "x"}}})
    assert validate_model_sections(cfg) == []


def test_level_fallback_matrix():
    assert LEVEL_FALLBACK == {
        "light": ("light", "default", "max"),
        "default": ("default", "max", "light"),
        "max": ("max", "default", "light"),
    }


@pytest.mark.parametrize("raw, expected", [
    ({"models": {"a": {}}}, False),                                   # одна модель резолвится сама
    ({"models": {}}, False),                                          # реестра нет — нечего резолвить
    ({"models": {"a": {}, "b": {}}}, True),                           # две и ни одного слота
    ({"models": {"a": {}, "b": {}}, "slots": {"default": "a"}}, False),
    ({"models": {"a": {}, "b": {}}, "slots": {"light": "a"}}, False),  # любой слот включает матрицу
    ({"models": {"a": {}, "b": {}}, "slots": {"max": "a"}}, False),
])
def test_default_slot_required_names_the_rule_once(raw, expected):
    """Правило §4 отдельным предикатом: его спрашивают двое — валидатор (жалуется)
    и слой записи (спрашивает ДО записи). Написанное дважды от руки, оно уже
    разъезжалось."""
    from config_modules.model_sections_validation import default_slot_required

    cfg = AppConfig.model_validate(raw)
    assert default_slot_required(cfg.models, cfg.slots) is expected
