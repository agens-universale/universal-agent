import pytest
from pydantic import ValidationError

from config_modules.model_registry import ModelEntry, AssignConfig
from config_modules.model_sections_validation import validate_model_sections
from config_modules.root import AppConfig, schema_error_details


def test_models_slots_assign_parse_minimal():
    cfg = AppConfig.model_validate({
        "models": {"glm": {"url": "http://x/v1/chat/completions", "token": ""}},
        "slots": {"default": "glm"},
        "assign": {"finish_gate": "light"},
    })
    assert cfg.models["glm"].model is None          # id = алиас (сахар)
    assert cfg.slots.default == "glm"


def test_reserved_alias_rejected():
    with pytest.raises(ValidationError, match="зарезервировано"):
        AppConfig.model_validate({"models": {"auto": {"url": "http://x", "token": ""}}})


def test_token_authorization_conflict():
    with pytest.raises(ValidationError, match="одно"):
        ModelEntry.model_validate({
            "token": "sk-1",
            "backend": {"chat_completion_headers": {"Authorization": "Bearer y"}},
        })


def test_url_completions_url_conflict():
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({"url": "http://a", "backend": {"completions_url": "http://b"}})


def test_own_url_without_token_is_rejected():
    """§2: свой адрес обязан идти со своим токеном — иначе общий ключ уходит на чужой хост."""
    from config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой адрес, но не задан токен"):
        ModelEntry.model_validate({"url": "https://evil/v1/chat/completions"})


def test_own_token_without_url_is_rejected():
    """Симметрия: свой токен без своего адреса уедет вслед за сменой общего адреса."""
    from config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        ModelEntry.model_validate({"token": "sk-own"})


def test_own_url_with_explicit_no_auth_is_valid():
    """`token: ""` — это ЗАДАННЫЙ токен («сервер не требует авторизации»), пара полна."""
    from config_modules.model_registry import ModelEntry
    entry = ModelEntry.model_validate({"url": "http://127.0.0.1:8014/v1/chat/completions",
                                       "token": ""})
    assert entry.authorization_header == ""


def test_second_spelling_of_address_needs_a_token():
    """`backend.completions_url` — то же «адрес задан», что и `url` (§2)."""
    from config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой адрес, но не задан токен"):
        ModelEntry.model_validate({"backend": {"completions_url": "https://evil/v1"}})


def test_second_spelling_of_token_needs_an_address():
    """Симметрия второго написания: заголовок Authorization — тоже «токен задан»."""
    from config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        ModelEntry.model_validate(
            {"backend": {"chat_completion_headers": {"Authorization": "Bearer x"}}})


def test_own_proxy_counts_as_own_address():
    """Решение владельца §2: прокси определяет, КУДА физически уходит запрос с токеном."""
    from config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой адрес, но не задан токен"):
        ModelEntry.model_validate({"backend": {"proxy": "http://attacker:8080"}})


def test_empty_url_is_not_an_own_address():
    """Рантайм резолвит адрес or-цепочкой (`model_router._resolve_alias`:
    `url → backend.completions_url → общий`), значит `url: ""` — это «своего адреса нет,
    пара наследуется». Правило обязано читать адрес так же, иначе отвергает законную
    форму."""
    entry = ModelEntry.model_validate({"url": ""})
    assert entry.url == ""
    assert entry.authorization_header is None      # токен наследуется вместе с адресом


def test_empty_url_with_a_token_is_still_half_a_pair():
    """Обратная сторона того же чтения: адреса нет, токен свой — половина, которая
    уедет вслед за сменой общего адреса. Определение «адрес задан» через `is not None`
    приняло бы эту запись как целую пару и пропустило мину."""
    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        ModelEntry.model_validate({"url": "", "token": "sk-own"})


def test_level_auto_friendly_error():
    with pytest.raises(ValidationError, match="авто-выбора"):
        AppConfig.model_validate({"level": "auto"})


def test_level_rejects_any_value_outside_the_three_levels():
    """`level` принимает ровно light|default|max в волнах A–B. У `auto` свой
    дружелюбный текст (тест выше); здесь — общая ветка: опечатка в ключе не
    должна протекать до Router'а, где «нет такого слота» превратилось бы в
    фоллбэк на default и молча работало бы не тем уровнем."""
    with pytest.raises(ValidationError, match="light"):
        AppConfig.model_validate({"level": "fast"})


def test_main_assign_forbidden():
    with pytest.raises(ValidationError):
        AssignConfig.model_validate({"main": "light"})


def test_unknown_assign_key_forbidden():
    """§14: «неизвестный ключ assign — строгая ошибка». Имя субагента стережёт
    фаза 2, а опечатку в имени ПОТРЕБИТЕЛЯ (`finish_gates`) ловить некому, кроме
    `extra="forbid"` схемы: без него строка тихо не действовала бы, а
    пользователь считал бы судью привязанным."""
    with pytest.raises(ValidationError):
        AssignConfig.model_validate({"finish_gates": "light"})


def test_one_model_may_stand_in_two_slots():
    """§14: «одна модель в двух слотах». Слоты — УКАЗАТЕЛИ, а не владение (§4),
    поэтому дублирующая ссылка легальна: типовой сетап «есть только быстрая и
    умная» ставит одну модель и на default, и на max."""
    cfg = AppConfig.model_validate({
        "models": {"fast": {"url": "http://fast/v1", "token": ""},
                   "smart": {"url": "http://smart/v1", "token": ""}},
        "slots": {"light": "fast", "default": "smart", "max": "smart"},
    })
    assert cfg.slots.default == "smart"
    assert cfg.slots.max == "smart"
    assert validate_model_sections(cfg) == []


def test_token_bearer_normalization():
    url = "http://x/v1"          # пара §2: токен без адреса — половина, схема её не примет
    assert ModelEntry.model_validate(
        {"url": url, "token": "sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate(
        {"url": url, "token": "Bearer sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate({"url": url, "token": ""}).authorization_header == ""


def test_schema_error_details_does_not_double_the_coordinate():
    """[ревью #4] `ModelEntry._validate_no_duplicate_settings` несёт координату
    в СВОЁМ тексте («models.<alias>: token и …» — «одна редакция на всех»,
    правило владельца), а pydantic ДОБАВЛЯЕТ свою через `loc` («models.opus41:
    …»). Склейка `schema_error_details` их не различала: результат называл
    координату дважды — «models.opus41: models.<alias>: token и …»."""
    try:
        AppConfig.model_validate({"models": {"opus41": {
            "token": "sk-secret",
            "backend": {"chat_completion_headers": {"Authorization": "Bearer y"}},
        }}})
    except ValidationError as exc:
        details = schema_error_details(exc)
    assert details == (
        "models.opus41: token и backend.chat_completion_headers.Authorization "
        "заданы одновременно — оставьте что-то одно"
    )
