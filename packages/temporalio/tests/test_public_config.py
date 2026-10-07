"""Публичный снимок настроек: вложенный дамп AppConfig без закрытых полей.

Закрытые поля — по метаданным схемы (``worker_only``/``secret``) плюс адрес
записи реестра моделей; снимок собирается обратно в те же настройки.
"""

import dataclasses
import json
from dataclasses import asdict

import pytest
from gc_core.config_modules.field_meta import leaf_meta
from gc_core.config_modules.root import AppConfig
from gc_core.context_preparation import settings_from_snapshot
from gc_core.model_router import ModelRouter
from gc_core.settings import (
    LoadedSettings,
    RuntimeEnv,
    _alias_path,
    is_explicit,
)
from gc_core_temporalio.public_config import (
    PublicConfigError,
    is_private_path,
    public_config_snapshot,
    settings_exclude_spec,
)

#: Каждое закрытое поле задано значением с меткой SECRET — метка не должна
#: дойти до снимка ни в каком виде.
RICH = {
    "backend": {
        "completions_url": "https://user:SECRET_ENDPOINT@private.ru/SECRET_PATH",
        "chat_completion_headers": {"Authorization": "SECRET_HEADER"},
        "use_ssl": False,
        "cert_path": "/SECRET/cert.pem",
        "key_path": "/SECRET/key.pem",
        "proxy": "https://SECRET_PROXY@host",
        "chat_completion_kwargs": {"temperature": 0.2},
    },
    "agent": {
        "client_name": "SECRET_CLIENT",
        "x_cl_id": "SECRET_CL_ID",
        "trace_id_header_name": "X-SECRET-TRACE",
        "trace_header_enabled": True,
        "harbor_exec_init": "export TOKEN=SECRET_SHELL",
        "max_steps": 7,
        "use_local_subagents": ["searcher"],
    },
    "mcp": {
        "servers": {
            "x": {
                "transport": "stdio",
                "command": "SECRET_COMMAND",
                "env": {"TOKEN": "SECRET_MCP"},
            }
        },
        "meta": {"recommended-mcp-manifest": "/SECRET/manifest"},
    },
    "tui": {
        "bang_shell": {"max_output_lines": 7},
        "file_view": {"max_file_bytes": 7},
    },
    "voice": {"url": "https://SECRET_VOICE", "token": "SECRET_VOICE"},
    "update": {"extra_args": ["--SECRET"]},
    "agent_auth": {"token": "SECRET_AUTH"},
    "level": "max",
    "models": {
        "fast": {
            "model": "provider-fast",
            "backend": {
                "completions_url": "https://SECRET_FAST.invalid/fast",
                "chat_completion_headers": {"Authorization": "SECRET_FAST"},
                "key_path": "/SECRET/fast.key",
                "chat_completions_max_tokens": 1234,
            },
        },
        "large": {
            "model": "provider-large",
            "url": "https://private.ru/SECRET_LARGE",
            "token": "SECRET_LARGE",
            "pricing": {"input": 2, "output": 3},
            "backend": {
                "proxy": "http://SECRET_LARGE_PROXY",
                "use_ssl": False,
            },
            "main": {"temperature": 0.2},
        },
    },
    "slots": {"light": "fast", "default": "fast", "max": "large"},
    "assign": {
        "command_judge": "fast",
        "subagents": {"searcher": "large"},
    },
}

RUNTIME = RuntimeEnv(
    config_path="/SECRET/config.yml",
    config_dir="/SECRET",
    active_profile="work",
    notices=("SECRET_NOTICE",),
    voice_overridden_by_managed="SECRET_VOICE_FIELD",
    launch_root="/workspace",
    project_root="/workspace",
    trace_id="trace-1",
    subagent_tools_allow=("fs_read_file",),
    subagent_tools_stop=("exec_command",),
    os_backend="harbor",
)


def rich():
    return LoadedSettings(
        settings=AppConfig.model_validate(RICH), runtime=RUNTIME
    )


def _values_at(document, keys):
    """Значения документа по пути ключей; ``*`` — любая запись словаря."""
    if not keys:
        return [document]
    if not isinstance(document, dict):
        return []
    head, *rest = keys
    if head == "*":
        return [
            value
            for item in document.values()
            for value in _values_at(item, rest)
        ]
    return _values_at(document[head], rest) if head in document else []


def _private_leaves():
    return [
        path for path in leaf_meta() if is_private_path(tuple(path.split(".")))
    ]


def test_private_paths_are_exactly_schema_metadata_plus_model_address():
    expected = {
        path
        for path, meta in leaf_meta().items()
        if meta.worker_only or meta.secret
    } | {"models.*.url"}
    assert set(_private_leaves()) == expected
    # Секрет без worker_only (models.*.token) в снимок тоже не идёт.
    assert "models.*.token" in expected


def test_snapshot_contains_no_worker_only_or_secret_field():
    loaded = rich()
    full = loaded.settings.model_dump(mode="json", by_alias=True)
    snapshot = public_config_snapshot(loaded)
    present, in_source = [], set()
    for path in _private_leaves():
        keys = _alias_path(AppConfig, path.split("."))
        if any(_values_at(full, keys)):
            in_source.add(path)
        if _values_at(snapshot.settings, keys):
            present.append(path)
    assert present == []
    # Проверка не пустая: закрытые поля в исходных настройках заданы.
    assert {
        "backend.proxy",
        "backend.chat_completion_headers",
        "agent.harbor_exec_init",
        "mcp.servers.*.env",
        "voice.token",
        "agent_auth",
        "models.*.url",
        "models.*.token",
        "models.*.backend.completions_url",
        "models.*.backend.proxy",
    } <= in_source
    serialized = json.dumps(asdict(snapshot), allow_nan=False)
    assert "SECRET" not in serialized
    assert "private.ru" not in serialized
    assert "agent_auth" not in snapshot.settings
    assert "mcp" not in snapshot.settings
    assert set(snapshot.runtime) == {
        "active_profile",
        "launch_root",
        "project_root",
        "trace_id",
        "is_tui",
        "text_answer_mode",
        "finish_gate_smart",
        "subagent_tools_allow",
        "subagent_tools_stop",
        "os_backend",
        "run_mode",
    }
    # Исходные настройки не тронуты.
    assert loaded.settings.agent_auth.model_dump() == {"token": "SECRET_AUTH"}


def test_snapshot_round_trips_into_app_config_and_runtime():
    loaded = rich()
    snapshot = public_config_snapshot(loaded)
    rebuilt = settings_from_snapshot(
        json.loads(json.dumps(snapshot.settings)),
        json.loads(json.dumps(snapshot.runtime)),
    )
    assert isinstance(rebuilt.settings, AppConfig)
    # Публичная часть совпадает до значения, включая явность полей.
    spec = settings_exclude_spec()
    dump = {
        "mode": "json",
        "by_alias": True,
        "exclude_unset": True,
        "exclude": spec,
    }
    assert rebuilt.settings.model_dump(**dump) == loaded.settings.model_dump(
        **dump
    )
    assert is_explicit(rebuilt.settings, "agent.max_steps")
    assert not is_explicit(rebuilt.settings, "agent.max_tool_calls")
    assert rebuilt.settings.agent.max_steps == 7
    assert rebuilt.settings.backend.completions_url == ""
    assert rebuilt.settings.models["large"].url is None
    assert rebuilt.settings.models["large"].token is None
    assert rebuilt.runtime == dataclasses.replace(
        RUNTIME,
        config_path=None,
        config_dir=None,
        notices=(),
        voice_overridden_by_managed=None,
    )
    # Снимок снимка — те же настройки (признаки транспорта — только у воркера).
    again = public_config_snapshot(rebuilt)
    assert (again.settings, again.runtime) == (
        snapshot.settings,
        snapshot.runtime,
    )


def test_default_settings_round_trip_and_transport_flags():
    snapshot = public_config_snapshot(AppConfig())
    assert snapshot.settings == {}
    assert snapshot.model_transport_configured is False
    assert snapshot.provider_currency == "$"
    rebuilt = settings_from_snapshot(snapshot.settings, snapshot.runtime)
    assert rebuilt.settings == AppConfig()
    assert rebuilt.runtime == RuntimeEnv()


def test_routing_identity_and_currencies_survive_without_transport():
    loaded = rich()
    public = public_config_snapshot(loaded)
    assert public.model_transport_configured is True
    assert public.provider_currency == "₽"
    worker_router = ModelRouter(loaded, level_provider=lambda: "max")
    public_router = ModelRouter(
        settings_from_snapshot(public.settings, public.runtime),
        level_provider=lambda: "max",
    )
    for consumer in ("main", "command_judge", "subagent:searcher"):
        worker, workflow = (
            worker_router.resolve(consumer),
            public_router.resolve(consumer),
        )
        assert (workflow.alias, workflow.model, workflow.source) == (
            worker.alias,
            worker.model,
            worker.source,
        )
        assert not workflow.url and not workflow.authorization
        assert workflow.tariff == worker.tariff
    # Параметры запроса алиаса (main) доходят до роутера workflow.
    assert public_router.resolve("main").backend_overrides == {}
    assert public_router.resolve("main").main == {"temperature": 0.2}
    assert public_router.resolve("command_judge").backend_overrides == {
        "chat_completions_max_tokens": 1234
    }
    assert public.settings["agent"]["use_local_subagents"] == ["searcher"]


@pytest.mark.parametrize(
    "overlay",
    [
        {"backend": {"chat_completion_kwargs": {"api_key": "SECRET"}}},
        {
            "chat_completions": {
                "auxiliary_llm": {
                    "completion_kwargs": {
                        "headers": {"Authorization": "SECRET"}
                    }
                }
            }
        },
        {
            "models": {
                "m": {
                    "model": "x",
                    "main": {"new_password": "SECRET"},
                }
            }
        },
        {
            "backend": {
                "chat_completion_kwargs": {
                    "reasoning": {"effort": "low", "token": "SECRET"}
                }
            }
        },
    ],
)
def test_unknown_provider_parameters_fail_without_value_in_exception(
    overlay,
):
    with pytest.raises(PublicConfigError) as error:
        public_config_snapshot(AppConfig.model_validate(overlay))
    assert "SECRET" not in str(error.value)


def test_known_provider_parameters_are_preserved():
    kwargs = {
        "temperature": 0.3,
        "stop": ["END"],
        "thinking": {"type": "enabled", "budget_tokens": 32},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "x", "schema": {"type": "object"}},
        },
    }
    settings = AppConfig.model_validate(
        {
            "backend": {"chat_completion_kwargs": kwargs},
            "chat_completions": {
                "auxiliary_llm": {
                    "judge_max_tokens": 123,
                    "completion_kwargs": {"thinking": kwargs["thinking"]},
                }
            },
        }
    )
    snapshot = public_config_snapshot(settings)
    assert snapshot.settings["backend"]["chat_completion_kwargs"] == kwargs
    aux = snapshot.settings["chat_completions"]["auxiliary_llm"]
    assert aux == {
        "judge_max_tokens": 123,
        "completion_kwargs": {"thinking": kwargs["thinking"]},
    }


def test_runtime_fields_require_classification_and_backend_kind():
    @dataclasses.dataclass(frozen=True)
    class NewRuntime(RuntimeEnv):
        new_secret_handle: str = "SECRET"

    with pytest.raises(PublicConfigError) as error:
        public_config_snapshot(
            LoadedSettings(settings=AppConfig(), runtime=NewRuntime())
        )
    assert "SECRET" not in str(error.value)
    with pytest.raises(PublicConfigError):
        public_config_snapshot(
            LoadedSettings(
                settings=AppConfig(), runtime=RuntimeEnv(os_backend="custom")
            )
        )


def test_model_auxiliary_and_os_backend_env_do_not_leak_into_history():
    """Параметры алиаса проходят ту же схему, что и глобальные; переменные
    окружения бэкенда (могут быть токены) в публичный снимок не идут."""
    with pytest.raises(PublicConfigError) as error:
        public_config_snapshot(
            AppConfig.model_validate(
                {
                    "models": {
                        "m": {"model": "x", "auxiliary": {"api_key": "SECRET"}}
                    }
                }
            )
        )
    assert "SECRET" not in str(error.value)
    snapshot = public_config_snapshot(
        AppConfig.model_validate(
            {"os_backend": {"kind": "local", "env": {"TOKEN": "SECRET"}}}
        )
    )
    assert "SECRET" not in json.dumps(snapshot.settings)


@pytest.mark.parametrize(
    "kind", ["local", "harbor", "docker", "ssh", "exec", "sandbox"]
)
def test_runtime_backend_kind_from_schema_is_public(kind):
    snapshot = public_config_snapshot(
        LoadedSettings(
            settings=AppConfig(), runtime=RuntimeEnv(os_backend=kind)
        )
    )
    assert snapshot.runtime["os_backend"] == kind


@pytest.mark.parametrize(
    "backend",
    [
        {"kind": "docker", "docker": {"name": "SECRET_CONTAINER"}},
        {"kind": "ssh", "ssh": {"host": "SECRET_HOST", "user": "SECRET_USER"}},
        {"kind": "exec", "exec": {"argv_prefix": ["SECRET_WRAPPER", "-x"]}},
    ],
)
def test_worker_only_os_backend_snapshot_round_trips(backend):
    """Подключение бэкенда ОС — только для воркера, но снимок обязан
    собираться обратно в настройки (валидатор требует поле при этом kind):
    в снимке заглушка, а не значение."""
    settings = AppConfig.model_validate({"os_backend": backend})
    snapshot = public_config_snapshot(
        LoadedSettings(
            settings=settings, runtime=RuntimeEnv(os_backend=backend["kind"])
        )
    )
    assert "SECRET" not in json.dumps(snapshot.settings)
    restored = settings_from_snapshot(snapshot.settings, snapshot.runtime)
    assert restored.settings.os_backend.kind == backend["kind"]
