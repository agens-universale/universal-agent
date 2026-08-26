import pytest
from pydantic import ValidationError

from gc_core.config_modules.model_registry import ModelEntry, AssignConfig
from gc_core.config_modules.root import AppConfig


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
    from gc_core.config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой адрес, но не задан токен"):
        ModelEntry.model_validate({"url": "https://evil/v1/chat/completions"})


def test_own_token_without_url_is_rejected():
    """Симметрия: свой токен без своего адреса уедет вслед за сменой общего адреса."""
    from gc_core.config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        ModelEntry.model_validate({"token": "sk-own"})


def test_own_url_with_explicit_no_auth_is_valid():
    """`token: ""` — это ЗАДАННЫЙ токен («сервер не требует авторизации»), пара полна."""
    from gc_core.config_modules.model_registry import ModelEntry
    entry = ModelEntry.model_validate({"url": "http://127.0.0.1:8014/v1/chat/completions",
                                       "token": ""})
    assert entry.authorization_header == ""


def test_second_spelling_of_address_needs_a_token():
    """`backend.completions_url` — то же «адрес задан», что и `url` (§2)."""
    from gc_core.config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой адрес, но не задан токен"):
        ModelEntry.model_validate({"backend": {"completions_url": "https://evil/v1"}})


def test_second_spelling_of_token_needs_an_address():
    """Симметрия второго написания: заголовок Authorization — тоже «токен задан»."""
    from gc_core.config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        ModelEntry.model_validate(
            {"backend": {"chat_completion_headers": {"Authorization": "Bearer x"}}})


def test_own_proxy_counts_as_own_address():
    """Решение владельца §2: прокси определяет, КУДА физически уходит запрос с токеном."""
    from gc_core.config_modules.model_registry import ModelEntry
    with pytest.raises(ValueError, match="задан свой адрес, но не задан токен"):
        ModelEntry.model_validate({"backend": {"proxy": "http://attacker:8080"}})


def test_level_auto_friendly_error():
    with pytest.raises(ValidationError, match="авто-выбора"):
        AppConfig.model_validate({"level": "auto"})


def test_main_assign_forbidden():
    with pytest.raises(ValidationError):
        AssignConfig.model_validate({"main": "light"})


def test_token_bearer_normalization():
    url = "http://x/v1"          # пара §2: токен без адреса — половина, схема её не примет
    assert ModelEntry.model_validate(
        {"url": url, "token": "sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate(
        {"url": url, "token": "Bearer sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate({"url": url, "token": ""}).authorization_header == ""
