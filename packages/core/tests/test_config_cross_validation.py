"""Межполевые проверки конфига: отказы схемы (однозначные поломки одного поля)
и стартовые notices (бессмысленные сочетания полей итогового мержа)."""

import pytest
import yaml
from gc_core.config import load_settings
from gc_core.config_modules import env_vars
from gc_core.config_modules.cross_validation import cross_field_notices
from gc_core.config_modules.root import AppConfig


def _notices(raw: dict) -> list[str]:
    return cross_field_notices(AppConfig.model_validate(raw))


# --- отказы: пустой путь в списках guard'ов ----------------------------------


@pytest.mark.parametrize(
    "section, field",
    [
        ("write_guard", "extra_paths"),
        ("read_guard", "allowed_read_roots"),
        ("read_guard", "allowed_read_paths"),
        ("read_guard", "denied_read_paths"),
        ("protected_paths", "extra"),
    ],
)
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_path_in_guard_list_is_rejected(section, field, blank):
    with pytest.raises(ValueError) as info:
        AppConfig.model_validate(
            {"security": {section: {field: ["/ok", blank]}}}
        )
    errors = info.value.errors()
    assert errors[0]["loc"] == ("security", section, field)
    assert "[1]" in errors[0]["msg"]


def test_non_blank_paths_pass():
    cfg = AppConfig.model_validate(
        {"security": {"read_guard": {"allowed_read_roots": ["*", "/data"]}}}
    )
    assert cfg.security.read_guard.allowed_read_roots == ["*", "/data"]


def test_empty_env_in_guard_path_fails_at_load(tmp_path, monkeypatch):
    """Типичный источник пустого пути — `${ENV:VAR:-}`, ставший пустотой:
    отказ называет dotted-путь поля, а не молча раскрывает его в cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_vars, "managed_config_path", lambda: tmp_path / "managed.yml"
    )
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)
    monkeypatch.delenv("GC_T_DIR", raising=False)
    base = tmp_path / "config.yml"
    base.write_text(
        yaml.safe_dump(
            {
                "security": {
                    "read_guard": {"allowed_read_paths": ["${ENV:GC_T_DIR:-}"]}
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match=r"security\.read_guard\.allowed_read_paths"
    ):
        load_settings(str(base))


# --- отказ: негодный regex маскировки ----------------------------------------


def test_invalid_redact_pattern_is_rejected():
    with pytest.raises(ValueError) as info:
        AppConfig.model_validate(
            {
                "logging": {
                    "redact_secrets": {"extra_patterns": ["ok\\d+", "(x"]}
                }
            }
        )
    err = info.value.errors()[0]
    assert err["loc"] == ("logging", "redact_secrets", "extra_patterns")
    assert "[1]" in err["msg"]


# --- notices -----------------------------------------------------------------


def test_defaults_produce_no_notices():
    assert cross_field_notices(AppConfig()) == []


def test_backend_half_mtls_pair():
    (notice,) = _notices({"backend": {"key_path": "/k.pem"}})
    assert notice.startswith("backend.key_path задан без backend.cert_path")


def test_model_mtls_pair_judged_with_inherited_half():
    base = {"backend": {"cert_path": "/c.pem", "key_path": "/k.pem"}}
    ok = {
        **base,
        "models": {"m": {"model": "x", "backend": {"key_path": "/m.pem"}}},
    }
    assert _notices(ok) == []  # cert унаследован — пара целая
    broken = {
        "models": {"m": {"model": "x", "backend": {"key_path": "/m.pem"}}}
    }
    assert any(n.startswith("models.m.backend") for n in _notices(broken))


def test_mcp_half_mtls_pair_and_missing_endpoint():
    notices = _notices(
        {
            "mcp": {
                "servers": {
                    "a": {
                        "transport": "stdio",
                        "command": "x",
                        "client-cert": "/c",
                    },
                    "b": {"transport": "stdio"},
                    "c": {"transport": "http"},
                }
            }
        }
    )
    assert any(
        n.startswith("mcp.servers.a.client-cert задан без") for n in notices
    )
    assert any(n.startswith("mcp.servers.b.command") for n in notices)
    assert any(n.startswith("mcp.servers.c.url") for n in notices)


def test_min_response_tokens_above_max_tokens():
    (notice,) = _notices(
        {
            "backend": {
                "chat_completions_max_tokens": 512,
                "chat_completions_min_response_tokens": 1024,
            }
        }
    )
    assert "backend.chat_completions_min_response_tokens (1024)" in notice


def test_model_override_of_max_tokens_checked_against_global_min():
    notices = _notices(
        {
            "models": {
                "small": {
                    "model": "x",
                    "backend": {"chat_completions_max_tokens": 256},
                }
            }
        }
    )
    assert any(n.startswith("models.small.backend.") for n in notices)


def test_turn_end_trigger_not_below_mid_trigger():
    raw = {
        "summary": {
            "chat_completions_summary_trigger_chars": 1000,
            "chat_completions_summary_turn_end_trigger_chars": 1000,
        }
    }
    (notice,) = _notices(raw)
    assert "summary.chat_completions_summary_turn_end_trigger_chars" in notice
    # Невыставленный порог конца хода выводится сам — не повод для notice.
    assert (
        _notices({"summary": {"chat_completions_summary_trigger_chars": 1000}})
        == []
    )


def test_project_memory_append_larger_than_memory():
    (notice,) = _notices(
        {"project_memory": {"max_chars": 100, "max_append_chars": 500}}
    )
    # Ключ и значение проверяются по отдельности: строка вида «пакет.атрибут»
    # читается сторожем литералов как ссылка на модуль.
    assert "max_append_chars" in notice and "(500)" in notice
    assert notice.split(".", 1)[0] == "project_memory"


def test_notices_reach_runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_vars, "managed_config_path", lambda: tmp_path / "managed.yml"
    )
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)
    base = tmp_path / "config.yml"
    base.write_text(
        yaml.safe_dump({"backend": {"cert_path": "/c.pem"}}), encoding="utf-8"
    )
    notices = load_settings(str(base)).runtime.notices
    assert any(n.startswith("backend.cert_path задан без") for n in notices)
