"""Метаданные полей настроек: флаги, наследование, маскировка, SecretStr."""

from gc_core.config_modules.field_meta import (
    MASK,
    REVEAL_SECRETS,
    FieldMeta,
    leaf_meta,
    meta_for,
    paths_where,
    plain_dump,
    redact,
    redacted_dump,
)
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import serialize_model_entry
from gc_testkit.settings import settings_from_flat
from pydantic import SecretStr

_SECRETS = {
    "backend": {
        "proxy": "http://user:proxy-pass@proxy:3128",
        "chat_completion_headers": {
            "Authorization": "Bearer hdr-SECRET",
            "X-Tenant": "tenant-SECRET",
        },
    },
    "voice": {"url": "http://stt", "token": "voice-SECRET"},
    "mcp": {
        "servers": {
            "srv": {
                "transport": "http",
                "url": "http://mcp",
                "token": "mcp-SECRET",
                "headers": {"X-Key": "mcp-hdr-SECRET"},
                "env": {"API_KEY": "mcp-env-SECRET"},
            }
        }
    },
    "models": {"m1": {"url": "http://m1", "token": "model-SECRET"}},
    "agent_auth": {"auth_endpoint": "http://auth", "login": "auth-SECRET"},
}
_VALUES = (
    "proxy-pass",
    "hdr-SECRET",
    "tenant-SECRET",
    "voice-SECRET",
    "mcp-SECRET",
    "mcp-hdr-SECRET",
    "mcp-env-SECRET",
    "model-SECRET",
    "auth-SECRET",
)


def _settings() -> AppConfig:
    return AppConfig.model_validate(_SECRETS)


# --- флаги и наследование ---------------------------------------------------


def test_section_flag_is_inherited_by_every_leaf():
    # mcp.servers помечен expert+worker_only на уровне секции.
    for path, meta in leaf_meta().items():
        if path.startswith("mcp.servers."):
            assert meta.expert and meta.worker_only, path
    # Флаг предка не снимается ниже: token — ещё и secret.
    token = meta_for("mcp.servers.*.token")
    assert token == FieldMeta(secret=True, worker_only=True, expert=True)


def test_star_pattern_matches_any_dict_entry_name():
    assert "models.*.token" in paths_where("secret")
    assert meta_for("models.gpt-5.token").secret
    assert meta_for("mcp.servers.github.token").secret
    assert not meta_for("models.gpt-5.url").secret


def test_dict_leaf_key_takes_flags_of_the_leaf():
    # Ключ внутри словаря-листа — флаги самого листа.
    assert meta_for("backend.chat_completion_headers.Authorization").secret
    assert meta_for("mcp.servers.s.env.API_KEY").secret
    assert not meta_for("backend.chat_completion_kwargs.temperature").secret


def test_section_without_declared_fields_is_a_leaf():
    # agent_auth: произвольные ключи — флаги даёт сама секция.
    assert "agent_auth" in paths_where("secret")
    assert meta_for("agent_auth.login").secret
    assert meta_for("agent_auth").worker_only


def test_section_meta_is_common_flag_of_its_leaves():
    assert meta_for("voice").worker_only
    assert not meta_for("voice").secret  # секрет — только voice.token
    assert meta_for("paths").restart is False
    assert paths_where("restart") == {
        "paths.threads_data_path",
        "paths.logs_dump_dir",
    }


def test_unknown_path_has_no_flags():
    assert meta_for("no.such.path") == FieldMeta()


# --- redact / redacted_dump -------------------------------------------------


def test_redacted_dump_masks_secrets():
    dump = redacted_dump(_settings())
    text = repr(dump)
    for value in _VALUES:
        assert value not in text, value
    assert dump["voice"]["token"] == MASK
    assert dump["models"]["m1"]["token"] == MASK
    assert dump["mcp"]["servers"]["srv"]["token"] == MASK
    assert dump["backend"]["proxy"] == MASK
    assert dump["agent_auth"] == {"auth_endpoint": MASK, "login": MASK}
    # не-секреты видны
    assert dump["voice"]["url"] == "http://stt"
    assert dump["models"]["m1"]["url"] == "http://m1"


def test_secret_dict_keeps_keys_visible():
    dump = redacted_dump(_settings())
    assert dump["backend"]["chat_completion_headers"] == {
        "Authorization": MASK,
        "X-Tenant": MASK,
    }
    assert dump["mcp"]["servers"]["srv"]["env"] == {"API_KEY": MASK}


def test_empty_secret_stays_visible():
    dump = redacted_dump(AppConfig())
    assert dump["voice"]["token"] == ""
    assert dump["backend"]["proxy"] is None
    assert dump["backend"]["chat_completion_headers"] == {}
    assert dump["agent_auth"] is None
    entry = redacted_dump(
        AppConfig.model_validate(
            {"models": {"local": {"url": "http://l", "token": ""}}}
        )
    )["models"]["local"]
    assert entry["token"] == ""  # «сервер не требует авторизации» — правда


def test_redact_on_plain_document():
    doc = {"voice": {"token": "t", "url": "u"}, "agent": {"model": "m"}}
    assert redact(doc) == {
        "voice": {"token": MASK, "url": "u"},
        "agent": {"model": "m"},
    }
    assert doc["voice"]["token"] == "t"  # вход не мутирован


# --- SecretStr --------------------------------------------------------------


def test_repr_and_str_never_contain_secret_values():
    settings = _settings()
    for text in (repr(settings), str(settings)):
        for value in _VALUES:
            assert value not in text, value


def test_scalar_secrets_are_secret_str_with_raw_value_on_request():
    settings = _settings()
    assert isinstance(settings.voice.token, SecretStr)
    assert settings.voice.token.get_secret_value() == "voice-SECRET"
    entry = settings.models["m1"]
    assert entry.authorization_header == "Bearer model-SECRET"
    assert settings.model_dump(mode="json")["voice"]["token"] != (
        "voice-SECRET"
    )
    revealed = settings.model_dump(mode="json", context=REVEAL_SECRETS)
    assert revealed["voice"]["token"] == "voice-SECRET"
    assert revealed["models"]["m1"]["token"] == "model-SECRET"
    server = plain_dump(settings.mcp.servers["srv"], by_alias=True)
    assert server["token"] == "mcp-SECRET"


def test_plain_dumps_carry_secrets_and_test_bridge_round_trips():
    settings = _settings()
    # Проекция записи для ModelRouter несёт уже нормализованный заголовок.
    projected = serialize_model_entry(settings.models["m1"])
    assert projected["authorization_header"] == "Bearer model-SECRET"
    revealed = settings.model_dump(by_alias=True, context=REVEAL_SECRETS)
    back = settings_from_flat(revealed).settings
    assert back == settings
    assert back.voice.token.get_secret_value() == "voice-SECRET"
    assert back.mcp.servers["srv"] == settings.mcp.servers["srv"]
    # Прежняя форма записи модели (проекция роутера) переводится обратно в token.
    from_projection = settings_from_flat({"models": {"m1": projected}})
    assert (
        serialize_model_entry(from_projection.settings.models["m1"])
        == projected
    )


def test_python_dump_round_trip_keeps_secret_values():
    settings = _settings()
    rebuilt = AppConfig.model_validate(settings.model_dump(by_alias=True))
    assert rebuilt == settings
    assert rebuilt.models["m1"].token.get_secret_value() == "model-SECRET"
