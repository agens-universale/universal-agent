"""Типизированные настройки Harbor передают подключение и параметры моделей в Router.

Builder проверяет реестр схемой и сохраняет правило пары «адрес + токен»,
раскрывая ENV-маркеры до валидации. Профиль без собственного подключения
наследует адрес раннера; явно пустой token отключает авторизацию.
"""

import importlib.util
import sys

import pytest
from gc_testkit.paths import HARBOR

from gc_core.model_router import ModelRouter


def _config_module():
    """Модуль сборки конфига. Он намеренно свободен от импортов harbor."""
    sys.path.insert(0, str(HARBOR))
    spec = importlib.util.spec_from_file_location(
        "adapters_gc_cli_config_for_models_tests",
        HARBOR / "adapters" / "gc_cli_config.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build(tmp_path, overrides):
    return _config_module().build_gc_cli_settings(
        completions_url="http://127.0.0.1:8100/v1/chat/completions",  # Polar пода
        model="polar-model",
        prompts_dir=tmp_path / "prompts",
        skills_dir=tmp_path / "skills",
        logs_dir=tmp_path / "logs",
        config_dir=str(tmp_path),
        overrides=overrides,
    )


def _resolve(config, level):
    return ModelRouter(config, level_provider=lambda: level).resolve(
        "main", check_breaker=False
    )


def test_token_of_a_profile_model_reaches_the_call(tmp_path):
    config = _build(
        tmp_path,
        {
            "models": {
                "weak": {
                    "model": "Qwen3.6-35B-A3B",
                    "url": "https://llm.example/v1/chat/completions",
                    "token": "sk-weak",
                },
            },
            "slots": {"light": "weak", "default": "weak", "max": "weak"},
        },
    )

    resolved = _resolve(config, "light")

    assert resolved.url == "https://llm.example/v1/chat/completions"
    assert resolved.authorization == "sk-weak"


def test_main_parameters_of_a_profile_model_reach_the_call(tmp_path):
    config = _build(
        tmp_path,
        {
            "models": {
                "strong": {
                    "model": "DeepSeek-V4-Flash-0731",
                    "main": {"reasoning": {"max_tokens": 4096}},
                },
            },
            "slots": {"default": "strong"},
        },
    )

    resolved = _resolve(config, "default")

    assert resolved.main == {"reasoning": {"max_tokens": 4096}}
    assert resolved.backend_overrides == {}


def test_explicit_empty_token_means_no_authorization(tmp_path):
    """Прокси кластера без авторизации: свой адрес и `token: ""` — полная пара."""
    config = _build(
        tmp_path,
        {
            "models": {
                "strong": {
                    "model": "DeepSeek-V4-Flash-0731",
                    "url": "http://litellm-noauth.vllm-proxy:8000/v1/chat/completions",
                    "token": "",
                },
            },
            "slots": {"default": "strong"},
        },
    )

    resolved = _resolve(config, "default")

    assert (
        resolved.url
        == "http://litellm-noauth.vllm-proxy:8000/v1/chat/completions"
    )
    assert resolved.authorization == ""


def test_own_url_without_token_is_rejected_like_on_the_file_path(tmp_path):
    """Свой адрес без своего токена унёс бы общий ключ на чужой хост."""
    with pytest.raises(ValueError, match="models.weak"):
        _build(
            tmp_path,
            {
                "models": {
                    "weak": {
                        "model": "m",
                        "url": "https://other.example/v1/chat/completions",
                    }
                },
            },
        )


def test_model_only_entry_still_inherits_the_runner_address(tmp_path):
    """Запись без своего подключения наследует адрес раннера."""
    config = _build(
        tmp_path,
        {
            "models": {"bench-light": {"model": "qwen/qwen3.6-35b-a3b"}},
            "slots": {"light": "bench-light"},
        },
    )

    resolved = _resolve(config, "light")

    assert resolved.model == "qwen/qwen3.6-35b-a3b"
    assert resolved.url == "http://127.0.0.1:8100/v1/chat/completions"
    assert resolved.authorization is None


def test_env_marker_in_a_model_token_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("GC_TEST_MODEL_KEY", "sk-from-env")
    config = _build(
        tmp_path,
        {
            "models": {
                "weak": {
                    "model": "m",
                    "url": "https://llm.example/v1/chat/completions",
                    "token": "${ENV:GC_TEST_MODEL_KEY}",
                }
            },
            "slots": {"light": "weak"},
        },
    )

    assert _resolve(config, "light").authorization == "sk-from-env"


def test_unset_env_that_breaks_the_pair_names_the_variable(
    tmp_path, monkeypatch
):
    """Адрес из незаданной переменной — пара без адреса: причина называется по имени."""
    monkeypatch.delenv("GC_TEST_MODEL_URL_ABSENT", raising=False)
    with pytest.raises(ValueError, match="GC_TEST_MODEL_URL_ABSENT"):
        _build(
            tmp_path,
            {
                "models": {
                    "weak": {
                        "model": "m",
                        "url": "${ENV:GC_TEST_MODEL_URL_ABSENT}",
                        "token": "sk",
                    }
                },
            },
        )


def test_unknown_key_in_a_model_entry_is_rejected(tmp_path):
    """Опечатка в записи (`tokn`) раньше проходила молча — теперь как на файловом пути."""
    with pytest.raises(ValueError):
        _build(tmp_path, {"models": {"weak": {"model": "m", "tokn": "sk"}}})
