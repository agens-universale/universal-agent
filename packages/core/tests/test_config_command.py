"""Команда ``config`` (gc_core.config_command): explain / validate / schema.

Стенд — настоящие файлы на диске: база с ``${ENV:…}`` и секретами, профиль
поверх неё, проектный оверлей. Окружение машины (GC_HOME, managed-слой, cwd)
изолировано тем же приёмом, что в test_config_layered.py.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gc_core import config_command
from gc_core.config_command import (
    EXIT_INVALID,
    EXIT_OK,
    EXIT_USAGE,
    UnknownKey,
    explain_entries,
    schema_document,
    schema_text,
)
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.config_modules.field_meta import MASK

TOKEN = "s3cr3t-token-value"
HEADER = "header-secret-value"

_BASE = """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
  chat_completion_headers:
    X-Api-Key: ${ENV:GC_TEST_HEADER}
agent:
  model: TestModel
  max_steps: 50
models:
  fast:
    model: gpt-x
    url: http://h/v1
    token: ${ENV:GC_TEST_TOKEN}
profile: work
"""


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """GC_HOME, платформа и cwd — во временном каталоге: managed-слой и
    проектный оверлей машины разработчика в стенд не попадают."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("GC_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("GC_TEST_HEADER", HEADER)
    return project


@pytest.fixture
def stack(isolated, tmp_path):
    """База (`${ENV}` + секреты) → проектный оверлей → профиль work."""
    conf = tmp_path / "conf"
    (conf / "profiles").mkdir(parents=True)
    base = conf / "config.yml"
    base.write_text(_BASE, encoding="utf-8")
    profile = conf / "profiles" / "work.yml"
    profile.write_text("agent:\n  max_steps: 7\n", encoding="utf-8")
    (isolated / ".gc").mkdir()
    project = isolated / ".gc" / "config.yml"
    project.write_text("agent:\n  verbose: true\n", encoding="utf-8")
    return {"base": base, "profile": profile, "project": project}


def _run(argv, program="gc-cli"):
    out, err = io.StringIO(), io.StringIO()
    code = config_command.main(argv, program=program, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _line(text: str, key: str) -> str:
    matches = [
        ln for ln in text.splitlines() if ln.strip().startswith(key + " ")
    ]
    assert matches, f"{key} нет в выводе:\n{text}"
    return matches[0]


# --- explain ---------------------------------------------------------------


def test_explain_lists_set_values_with_sources(stack):
    code, out, err = _run(["explain", "--config", str(stack["base"])])
    assert code == EXIT_OK, err
    assert "Профиль: work" in out
    assert str(stack["base"]) in out
    assert str(stack["profile"]) in out
    assert str(stack["project"].resolve()) in out
    assert "командной строки" in out  # CLI-слои сюда не входят
    assert "profile:work" in _line(out, "agent.max_steps")
    assert "= 7" in _line(out, "agent.max_steps")
    assert f"base ({stack['base']})" in _line(out, "agent.model")
    assert "project" in _line(out, "agent.verbose")
    # Группировка по секциям.
    assert "[agent]" in out and "[models]" in out
    # Умолчания без --all не показываются.
    assert "agent.subagents_max_count" not in out


def test_explain_masks_secrets_and_names_env_var(stack):
    code, out, _ = _run(["explain", "--config", str(stack["base"])])
    assert code == EXIT_OK
    assert TOKEN not in out and HEADER not in out
    token = _line(out, "models.fast.token")
    assert f'"{MASK}"' in token
    assert "${ENV:GC_TEST_TOKEN}" in token
    headers = _line(out, "backend.chat_completion_headers")
    assert '"X-Api-Key"' in headers  # ключи словаря-секрета видны
    assert f'"{MASK}"' in headers
    assert "${ENV:GC_TEST_HEADER}" in headers


def test_explain_json_never_contains_secret(stack):
    code, out, _ = _run(
        [
            "explain",
            "--config",
            str(stack["base"]),
            "--json",
            "--all",
            "--show-defaults",
        ]
    )
    assert code == EXIT_OK
    assert TOKEN not in out and HEADER not in out
    document = json.loads(out)
    assert document["profile"] == "work"
    assert document["files"]["base"] == str(stack["base"])
    assert document["files"]["profile"] == str(stack["profile"])
    assert document["files"]["managed"] is None
    by_key = {item["key"]: item for item in document["settings"]}
    token = by_key["models.fast.token"]
    assert token["value"] == MASK and token["secret"] is True
    assert token["sources"] == [
        {"layer": "base", "path": str(stack["base"]), "env": ["GC_TEST_TOKEN"]}
    ]
    steps = by_key["agent.max_steps"]
    assert steps["value"] == 7 and steps["default"] is False
    assert steps["sources"][0]["layer"] == "profile:work"
    assert isinstance(steps["default_value"], int)
    # --all: умолчания тоже, с источником default.
    untouched = by_key["agent.subagents_max_count"]
    assert untouched["default"] is True and untouched["source"] == "default"


def test_explain_all_includes_defaults(stack):
    code, out, _ = _run(["explain", "--config", str(stack["base"]), "--all"])
    assert code == EXIT_OK
    assert _line(out, "agent.subagents_max_count").endswith("← default")


def test_explain_show_defaults_prints_default_next_to_value(stack):
    code, out, _ = _run(
        [
            "explain",
            "--config",
            str(stack["base"]),
            "--show-defaults",
            "agent.max_steps",
        ]
    )
    assert code == EXIT_OK
    assert "(по умолчанию: " in _line(out, "agent.max_steps")


def test_explain_keys_accept_flat_name_section_and_dict_key(stack):
    code, out, _ = _run(
        [
            "explain",
            "--config",
            str(stack["base"]),
            "max_steps",
            "models.fast",
            "backend.chat_completion_headers.X-Api-Key",
        ]
    )
    assert code == EXIT_OK
    assert "profile:work" in _line(out, "agent.max_steps")
    assert _line(out, "models.fast.model")
    assert _line(out, "models.fast.pricing").endswith("← default")
    inner = _line(out, "backend.chat_completion_headers.X-Api-Key")
    assert f'"{MASK}"' in inner and HEADER not in out


def test_explain_unknown_key_suggests_and_exits_2(stack):
    code, out, err = _run(
        ["explain", "--config", str(stack["base"]), "agent.max_stesp"]
    )
    assert code == EXIT_USAGE
    assert out == ""
    assert "agent.max_stesp" in err
    assert "agent.max_steps" in err


def test_explain_profile_override(stack, tmp_path):
    other = tmp_path / "conf" / "profiles" / "other.yml"
    other.write_text("agent:\n  max_steps: 3\n", encoding="utf-8")
    code, out, _ = _run(
        [
            "explain",
            "--config",
            str(stack["base"]),
            "--profile",
            "other",
            "agent.max_steps",
        ]
    )
    assert code == EXIT_OK
    assert "Профиль: other" in out
    assert "profile:other" in _line(out, "agent.max_steps")


@pytest.mark.usefixtures("isolated")
def test_explain_broken_config_exits_1(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("agent:\n  max_steps: abc\n", encoding="utf-8")
    code, out, err = _run(["explain", "--config", str(bad)])
    assert code == EXIT_INVALID
    assert out == ""
    assert "agent.max_steps" in err


def test_explain_entries_unknown_key_raises(stack):
    from gc_core.config import load_settings

    loaded = load_settings(str(stack["base"]))
    with pytest.raises(UnknownKey) as info:
        explain_entries(loaded, ["agnt"])
    assert "agent" in info.value.suggestions


@pytest.mark.usefixtures("isolated")
def test_explain_model_alias_with_dot_keeps_token_masked(tmp_path):
    """Алиас модели с точкой: секретность решает путь схемы, а не строка пути."""
    base = tmp_path / "config.yml"
    base.write_text(
        "models:\n  gpt-4.1:\n    model: m\n    url: http://h\n"
        f"    token: {TOKEN}\n",
        encoding="utf-8",
    )
    code, out, _ = _run(["explain", "--config", str(base), "--json"])
    assert code == EXIT_OK
    assert TOKEN not in out


# --- validate --------------------------------------------------------------


def test_validate_ok(stack):
    code, out, err = _run(["validate", "--config", str(stack["base"])])
    assert code == EXIT_OK, err
    assert out.startswith("ok: ")
    assert str(stack["base"]) in out
    assert "профиль work" in out


@pytest.mark.usefixtures("isolated")
def test_validate_invalid_explains_and_exits_1(tmp_path):
    bad = tmp_path / "bad.yml"
    # Лишний ключ (`nope`) — только уведомление; отказ даёт негодное значение.
    bad.write_text("agent:\n  max_steps: abc\n  nope: 1\n", encoding="utf-8")
    code, out, err = _run(["validate", "--config", str(bad)], program="gc-ua")
    assert code == EXIT_INVALID
    assert out == ""
    assert "agent.max_steps" in err
    assert str(bad) in err


@pytest.mark.usefixtures("isolated")
def test_validate_json(tmp_path, stack):
    code, out, _ = _run(["validate", "--config", str(stack["base"]), "--json"])
    assert code == EXIT_OK
    document = json.loads(out)
    assert document["ok"] is True and document["error"] is None
    assert document["profile"] == "work"

    bad = tmp_path / "bad.yml"
    bad.write_text("agent: [1, 2\n", encoding="utf-8")
    code, out, _ = _run(["validate", "--config", str(bad), "--json"])
    assert code == EXIT_INVALID
    document = json.loads(out)
    assert document["ok"] is False and document["error"]


def test_validate_reports_missing_profile(stack):
    code, _, err = _run(
        ["validate", "--config", str(stack["base"]), "--profile", "nope"]
    )
    assert code == EXIT_INVALID
    assert "nope" in err


# --- schema ----------------------------------------------------------------


def test_schema_has_dialect_title_and_gc_metadata():
    document = schema_document()
    assert document["$schema"] == config_command.JSON_SCHEMA_DIALECT
    assert document["title"] == config_command.SCHEMA_TITLE
    token = document["$defs"]["ModelEntry"]["properties"]["token"]
    assert token["x-gc"]["secret"] is True
    # Ключи документа — alias, как в config.yml.
    meta = document["$defs"]["McpMetaConfig"]["properties"]
    assert "tools-cache-ttl" in meta


def test_schema_text_is_sorted_and_parses_back():
    text = schema_text()
    assert text.endswith("\n")
    assert json.loads(text) == schema_document()
    assert (
        text
        == json.dumps(
            json.loads(text), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )


def test_schema_is_deterministic_across_processes():
    """Порядок ключей не зависит от рандомизации хешей: два процесса с разным
    PYTHONHASHSEED дают байт-в-байт одну схему."""
    script = "from gc_core.config_command import schema_text; import sys; sys.stdout.write(schema_text())"
    outputs = []
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == schema_text()


def test_schema_output_file(tmp_path):
    target = tmp_path / "config.schema.json"
    code, out, _ = _run(["schema", "--output", str(target)])
    assert code == EXIT_OK
    assert str(target) in out
    assert Path(target).read_text(encoding="utf-8") == schema_text()


def test_schema_stdout():
    code, out, _ = _run(["schema"])
    assert code == EXIT_OK
    assert json.loads(out)["title"] == config_command.SCHEMA_TITLE


# --- разбор аргументов -----------------------------------------------------


def test_help_lists_subcommands():
    code, out, err = _run(["--help"])
    assert code == EXIT_OK
    for name in ("explain", "validate", "schema"):
        assert name in out
    assert "gc-cli config" in out
    assert err == ""


def test_no_subcommand_is_usage_error():
    code, out, err = _run([], program="gc-ua")
    assert code == EXIT_USAGE
    assert "gc-ua config" in err and out == ""


def test_unknown_option_is_usage_error():
    code, _, err = _run(["explain", "--bogus"])
    assert code == EXIT_USAGE
    assert "ошибка" in err
