"""Строгий `${ENV:VAR}`/`${FILE:path}` на старте и умолчания `:-`.

Старт (`load_settings`) отказывает списком ВСЕХ незаданных обязательных ссылок
(файл + dotted-ключ); мягкие витрины (`resolve_env_placeholders` без
`unresolved`, `resolve_env_only`) по-прежнему подставляют пустоту.
"""

import logging

import pytest
import yaml
from gc_core import onboarding
from gc_core.config import apply_cli_overrides, load_settings
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_interpolation import (
    UnresolvedPlaceholdersError,
    resolve_env_only,
    resolve_env_placeholders,
    resolve_env_placeholders_strict,
)
from gc_core.config_raw import missing_env_refs
from gc_testkit.settings import settings_from_flat


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Изолированный стек: база в tmp, managed — отсутствующий файл, проектный
    слой — по требованию теста."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_vars, "managed_config_path", lambda: tmp_path / "managed.yml"
    )
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)
    for name in ("GC_T_A", "GC_T_B", "GC_T_C", "GC_T_SET"):
        monkeypatch.delenv(name, raising=False)

    def write(name: str, body: dict):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
        return path

    return write


# --- синтаксис :- ------------------------------------------------------------


def test_default_used_when_unset(monkeypatch):
    monkeypatch.delenv("GC_T_A", raising=False)
    assert resolve_env_placeholders("${ENV:GC_T_A:-fallback}") == "fallback"
    assert resolve_env_placeholders("x${ENV:GC_T_A:-}y") == "xy"


def test_default_used_when_empty_like_shell(monkeypatch):
    monkeypatch.setenv("GC_T_A", "")
    assert resolve_env_placeholders("${ENV:GC_T_A:-d}") == "d"


def test_value_wins_over_default(monkeypatch):
    monkeypatch.setenv("GC_T_A", "real")
    assert resolve_env_placeholders("${ENV:GC_T_A:-d}") == "real"


def test_default_may_contain_colon_and_slash(monkeypatch):
    monkeypatch.delenv("GC_T_A", raising=False)
    assert (
        resolve_env_placeholders("${ENV:GC_T_A:-http://h:8080/v1}")
        == "http://h:8080/v1"
    )


def test_optional_ref_never_warns_or_counts_as_missing(monkeypatch, caplog):
    monkeypatch.delenv("GC_T_A", raising=False)
    with caplog.at_level(logging.WARNING):
        resolve_env_placeholders("${ENV:GC_T_A:-}")
    assert "GC_T_A" not in caplog.text
    assert missing_env_refs("${ENV:GC_T_A:-}") == []
    assert missing_env_refs("${ENV:GC_T_A}") == ["GC_T_A"]


def test_env_only_understands_default(monkeypatch):
    monkeypatch.delenv("GC_T_A", raising=False)
    assert resolve_env_only("${ENV:GC_T_A:-d}") == "d"


def test_file_default_when_file_missing(tmp_path):
    missing = tmp_path / "nope"
    assert resolve_env_placeholders(f"${{FILE:{missing}:-dflt}}") == "dflt"
    assert resolve_env_placeholders_strict(f"${{FILE:{missing}:-}}") == ""


def test_file_with_default_still_reads_existing_file(tmp_path):
    f = tmp_path / "tok"
    f.write_text("value\n")
    assert resolve_env_placeholders(f"${{FILE:{f}:-dflt}}") == "value"


# --- строгий резолвер ---------------------------------------------------------


def test_strict_collects_every_ref_with_dotted_keys(tmp_path):
    missing = tmp_path / "nofile"
    with pytest.raises(UnresolvedPlaceholdersError) as info:
        resolve_env_placeholders_strict(
            {
                "a": {"b": "${ENV:GC_T_A}"},
                "lst": ["ok", "x ${ENV:GC_T_B}"],
                "f": f"${{FILE:{missing}}}",
            },
            source="cfg.yml",
        )
    refs = {(r.kind, r.name, r.key) for r in info.value.refs}
    assert refs == {
        ("env", "GC_T_A", "a.b"),
        ("env", "GC_T_B", "lst[1]"),
        ("file", str(missing), "f"),
    }
    message = str(info.value)
    assert "cfg.yml: a.b" in message
    assert "${ENV:VAR:-" in message  # подсказка про необязательное значение


def test_strict_mode_does_not_log_missing(caplog):
    with caplog.at_level(logging.WARNING):
        with pytest.raises(UnresolvedPlaceholdersError):
            resolve_env_placeholders_strict("${ENV:GC_T_A}")
    assert "env var not set" not in caplog.text


def test_lenient_default_mode_unchanged(caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_env_placeholders({"a": "${ENV:GC_T_A}"}) == {"a": ""}
    assert "GC_T_A" in caplog.text


# --- load_settings -------------------------------------------------------------


def test_load_settings_fails_on_missing_env_naming_file_and_key(stack):
    base = stack(
        "config.yml",
        {
            "backend": {
                "chat_completion_headers": {
                    "Authorization": "Bearer ${ENV:GC_T_A}"
                }
            },
            "voice": {"token": "${ENV:GC_T_B}"},
        },
    )
    with pytest.raises(UnresolvedPlaceholdersError) as info:
        load_settings(str(base))
    message = str(info.value)
    assert "GC_T_A" in message and "GC_T_B" in message
    assert f"{base}: backend.chat_completion_headers.Authorization" in message
    assert f"{base}: voice.token" in message
    assert {r.layer for r in info.value.refs} == {"base"}


def test_load_settings_reports_all_layers_at_once(stack, monkeypatch):
    base = stack(
        "config.yml",
        {"profile": "work", "agent": {"model": "${ENV:GC_T_A}"}},
    )
    project = stack(".gc/config.yml", {"agent": {"x_cl_id": "${ENV:GC_T_B}"}})
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: project)
    stack("profiles/work.yml", {"agent": {"client_name": "${ENV:GC_T_C}"}})
    with pytest.raises(UnresolvedPlaceholdersError) as info:
        load_settings(str(base))
    by_name = {r.name: r for r in info.value.refs}
    assert set(by_name) == {"GC_T_A", "GC_T_B", "GC_T_C"}
    assert by_name["GC_T_A"].layer == "base"
    assert by_name["GC_T_B"].layer == "project"
    assert by_name["GC_T_C"].layer == "profile:work"
    assert by_name["GC_T_C"].key == "agent.client_name"


def test_overridden_missing_env_does_not_fail(stack, monkeypatch):
    """Незаданная переменная, чьё значение перекрыто верхним слоем, ни на что
    не влияет: отказ — только по ссылкам, дошедшим до итогового мержа."""
    base = stack(
        "config.yml",
        {
            "profile": "work",
            "backend": {
                "chat_completion_headers": {
                    "Authorization": "Bearer ${ENV:GC_T_A}",
                    "X-Trace": "${ENV:GC_T_B}",
                }
            },
            "agent": {"client_name": "${ENV:GC_T_C}"},
        },
    )
    project = stack(".gc/config.yml", {"agent": {"client_name": "project"}})
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: project)
    stack(
        "profiles/work.yml",
        {
            "backend": {
                "chat_completion_headers": {"Authorization": "Bearer t"}
            }
        },
    )
    with pytest.raises(UnresolvedPlaceholdersError) as info:
        load_settings(str(base))
    assert [r.name for r in info.value.refs] == ["GC_T_B"]

    monkeypatch.setenv("GC_T_B", "trace")
    loaded = load_settings(str(base))
    headers = loaded.settings.backend.chat_completion_headers
    assert headers["Authorization"] == "Bearer t"
    assert loaded.settings.agent.client_name == "project"
    source = loaded.provenance.source("agent.client_name")
    assert "\x00" not in repr(loaded.provenance.__dict__) and source.path


def test_overridden_ref_keeps_pair_rule_of_its_document(stack, monkeypatch):
    """Перекрытая ссылка не роняет загрузку, но и не отключает правило пары
    своего документа: токен без адреса в базе остаётся ошибкой (fail-closed)."""
    base = stack(
        "config.yml",
        {
            "models": {
                "m": {"model": "x", "url": "${ENV:GC_T_A}", "token": "t"}
            }
        },
    )
    project = stack(
        ".gc/config.yml",
        {
            "models": {
                "m": {
                    "url": "https://p.example/v1/chat/completions",
                    "token": "p",
                }
            }
        },
    )
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: project)
    with pytest.raises(ValueError) as info:
        load_settings(str(base))
    assert "GC_T_A" in str(info.value)
    assert "models.m" in str(info.value)


def test_shipped_demo_profile_needs_only_used_keys(stack, monkeypatch):
    from pathlib import Path

    import gc_core

    demo = Path(gc_core.__file__).parents[4] / "demo_profile.yml"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    base = stack("config.yml", {"profile": "demo"})
    stack("profiles/demo.yml", yaml.safe_load(demo.read_text("utf-8")))
    assert load_settings(str(base)).runtime.active_profile == "demo"


def test_missing_env_wins_over_its_own_consequence(stack):
    """Пустая подстановка ломает правило пары записи реестра — но причина
    одна, незаданная переменная, и отказ называет её, а не пару."""
    # Пустой токен — законное «без авторизации», поэтому ломаем адрес:
    base = stack(
        "config.yml",
        {
            "models": {
                "m": {"model": "x", "url": "${ENV:GC_T_A}", "token": "t"}
            }
        },
    )
    with pytest.raises(UnresolvedPlaceholdersError) as info:
        load_settings(str(base))
    assert [r.name for r in info.value.refs] == ["GC_T_A"]


def test_optional_and_set_refs_load(stack, monkeypatch):
    monkeypatch.setenv("GC_T_SET", "client-x")
    base = stack(
        "config.yml",
        {
            "agent": {
                "client_name": "${ENV:GC_T_SET}",
                "x_cl_id": "${ENV:GC_T_A:-42}",
            },
            "voice": {"token": "${ENV:GC_T_B:-}"},
        },
    )
    settings = load_settings(str(base)).settings
    assert settings.agent.client_name == "client-x"
    assert settings.agent.x_cl_id == "42"


def test_missing_file_ref_is_strict_too(stack, tmp_path):
    missing = tmp_path / "secrets" / "token"
    base = stack("config.yml", {"voice": {"token": f"${{FILE:{missing}}}"}})
    with pytest.raises(UnresolvedPlaceholdersError) as info:
        load_settings(str(base))
    (ref,) = info.value.refs
    assert ref.kind == "file" and ref.name == str(missing)
    assert "не найден" in str(info.value)


def test_cli_param_is_strict():
    with pytest.raises(UnresolvedPlaceholdersError) as info:
        apply_cli_overrides(
            settings_from_flat({}),
            extra_overrides={"completions_url": "${ENV:GC_T_A}"},
        )
    assert "--param: completions_url" in str(info.value)


def test_cli_param_optional_default():
    loaded = apply_cli_overrides(
        settings_from_flat({}),
        extra_overrides={"completions_url": "${ENV:GC_T_A:-http://d/v1}"},
    )
    assert loaded.settings.backend.completions_url == "http://d/v1"


def test_prewrite_validation_stays_lenient(monkeypatch):
    """Запись судят по файлу, а не по окружению этого процесса: ссылка на
    переменную, заданную только в окружении сервиса, записывается."""
    from gc_core.config import validate_config_text

    monkeypatch.delenv("GC_T_A", raising=False)
    validate_config_text("voice:\n  token: ${ENV:GC_T_A}\n")


# --- объяснение и лестница починки -------------------------------------------


def test_explain_broken_config_tells_to_set_the_variable(stack):
    base = stack("config.yml", {"voice": {"token": "${ENV:GC_T_A}"}})
    with pytest.raises(ValueError) as info:
        load_settings(str(base))
    text = onboarding.explain_broken_config(str(base), info.value)
    assert "export GC_T_A=" in text
    assert f"{base}: voice.token" in text
    assert "${ENV:VAR:-" in text
    # Откат к шаблону переменную не задаст — его не советуем.
    assert "--reset-config" not in text


def test_repair_message_summarises_missing_variables(stack):
    base = stack("config.yml", {"voice": {"token": "${ENV:GC_T_A}"}})
    with pytest.raises(ValueError) as info:
        load_settings(str(base))
    message = onboarding._repair_message(
        onboarding.REPAIR_STAGE_TEMPLATE, info.value, ""
    )
    assert "не заданы переменные окружения: GC_T_A" in message
    assert "\n" not in message
