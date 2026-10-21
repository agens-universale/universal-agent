"""Таймауты и повторы activities настраиваются по уровням, как у Pydantic AI."""

import socket
import ssl
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest
from gc_core.execution.contracts import ModelFailure, ModelRequest
from gc_core_temporalio.activities import (
    is_transient_provider_failure,
    retried_by_temporal,
)
from gc_core_temporalio.activity_config import (
    NON_RETRYABLE_ERROR_TYPES,
    ActivitiesConfig,
    activity_options,
    resolve_activity_settings,
)
from gc_core_temporalio.model_error_encoding import encode_model_failure
from gc_core_temporalio.provider import WorkerConfig
from pydantic import ValidationError
from temporalio.converter import DataConverter, value_to_type


def test_levels_merge_and_framework_non_retryable_types_are_kept():
    settings = resolve_activity_settings(
        ActivitiesConfig.model_validate(
            {
                "default": {"retry": {"maximum_interval": "PT30S"}},
                "model": {
                    "schedule_to_close": "PT10M",
                    "retry": {"non_retryable_error_types": ["Mine"]},
                },
                "tools": {"exec_command": {"start_to_close": 7200}},
            }
        )
    )
    model = settings["model"]
    assert model["schedule_to_close"] == 600
    assert model["retry"]["maximum_attempts"] == 0  # без ограничения
    assert model["retry"]["maximum_interval"] == 30
    assert model["retry"]["non_retryable_error_types"][0] == "Mine"
    assert set(NON_RETRYABLE_ERROR_TYPES) <= set(
        model["retry"]["non_retryable_error_types"]
    )
    assert settings["tools"]["exec_command"]["start_to_close"] == 7200
    # Эффект — одна попытка по умолчанию; повторы — только явно у инструмента.
    options = activity_options(settings["resource"], effect=True)
    assert options["retry_policy"].maximum_attempts == 1
    options = activity_options(settings["tools"]["exec_command"], effect=True)
    assert options["retry_policy"].maximum_attempts == 1
    explicit = resolve_activity_settings(
        ActivitiesConfig.model_validate(
            {"tools": {"exec_command": {"retry": {"maximum_attempts": 3}}}}
        )
    )
    options = activity_options(explicit["tools"]["exec_command"], effect=True)
    assert options["retry_policy"].maximum_attempts == 3


def test_activity_config_is_validated_on_worker_start(tmp_path):
    with pytest.raises(ValidationError):
        WorkerConfig.model_validate(
            {
                "state_dir": str(tmp_path),
                "profiles": {},
                "activities": {"model": {"retry": {"maximum_atempts": 3}}},
            }
        )


@pytest.mark.parametrize(
    ("details", "flags", "transient"),
    [
        ({"transport": True}, {}, True),  # обрыв транспорта
        # Неизвестный сбой без кода — не повторять без конца.
        ({}, {}, False),
        # Стрим/embedded без кода вовсе — транзиентный, как в ядре.
        ({"status_code": None, "raw_code": None}, {}, True),
        ({"status_code": 503}, {}, True),
        ({"status_code": 429}, {}, True),
        ({"status_code": 400}, {}, False),
        ({"status_code": 401}, {"auth_error": True}, False),
        ({"status_code": 400}, {"context_overflow": True}, False),
        ({"status_code": None, "raw_code": "insufficient_quota"}, {}, False),
        ({"alias": "a", "model": "m", "reason": "open"}, {}, False),
        ({"permanent": "dns"}, {}, False),
    ],
)
def test_transient_provider_failures(details, flags, transient):
    failure = ModelFailure("provider", "E", "m", details, **flags)
    assert is_transient_provider_failure(failure) is transient


def _connect_error(cause):
    try:
        try:
            raise cause
        except OSError as exc:
            raise httpx.ConnectError(str(exc)) from exc
    except httpx.ConnectError as exc:
        return exc


@pytest.mark.parametrize(
    ("make", "permanent"),
    [
        # Отказ соединения и временный сбой DNS проходят сами — повтор.
        (lambda: _connect_error(ConnectionRefusedError(61, "refused")), None),
        (
            lambda: _connect_error(
                socket.gaierror(socket.EAI_AGAIN, "temporary failure")
            ),
            None,
        ),
        (
            lambda: _connect_error(
                socket.gaierror(
                    socket.EAI_NONAME,
                    "nodename nor servname provided, or not known",
                )
            ),
            "dns",
        ),
        (
            lambda: _connect_error(
                ssl.SSLCertVerificationError(
                    1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed"
                )
            ),
            "tls",
        ),
        (
            lambda: httpx.UnsupportedProtocol(
                "Request URL is missing an 'http://' or 'https://' protocol."
            ),
            "invalid_url",
        ),
    ],
)
def test_transport_failures_that_retry_cannot_fix_are_not_transient(
    make, permanent
):
    failure = encode_model_failure(make())
    assert failure.details.get("permanent") == permanent
    assert is_transient_provider_failure(failure) is (permanent is None)


def test_default_build_id_is_version_plus_code_hash():
    from importlib.metadata import version

    from gc_core_temporalio.provider import WorkerVersioningConfig

    build = WorkerVersioningConfig().resolved_build_id()
    release, _, digest = build.partition("+")
    assert release == version("gc-core-temporalio")
    assert len(digest) == 12 and int(digest, 16) >= 0
    assert WorkerVersioningConfig(build_id="b1").resolved_build_id() == "b1"


def test_stream_and_embedded_errors_without_code_are_retried_like_core():
    from gc_core.utils.backend_errors import (
        EmbeddedChoiceError,
        is_retriable_backend_error,
    )
    from gc_core.utils.chat_completions_stream import BackendStreamError

    for exc in (
        BackendStreamError("stream cut", status_code=None),
        EmbeddedChoiceError("empty choice", status_code=None, raw_code=None),
    ):
        failure = encode_model_failure(exc)
        assert is_retriable_backend_error(exc)
        assert is_transient_provider_failure(failure), exc
    quota = EmbeddedChoiceError(
        "quota", status_code=None, raw_code="insufficient_quota"
    )
    assert not is_transient_provider_failure(encode_model_failure(quota))


def test_native_retry_policy_never_makes_a_tool_effect_retry_forever(
    tmp_path, monkeypatch
):
    """RetryPolicy() по умолчанию — maximum_attempts=0 («без ограничения»).
    У инструмента это не бесконечный повтор эффекта: настроили паузу — эффект
    остаётся одной попыткой; повторы — только явным числом."""
    from datetime import timedelta

    from gc_core.thread_store_file_durable import DurableFileThreadStore
    from gc_core_temporalio import AgentPlugin
    from gc_core_temporalio.activity_config import (
        activity_options,
        resolve_activity_settings,
    )
    from temporalio.common import RetryPolicy
    from temporalio.workflow import ActivityConfig

    monkeypatch.chdir(tmp_path)
    plugin = AgentPlugin(
        {"a": {}},
        thread_store=DurableFileThreadStore(str(tmp_path / "t")),
        tool_activities={
            "exec_command": ActivityConfig(
                retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=5))
            ),
            "fs_write_file": ActivityConfig(
                retry_policy=RetryPolicy(maximum_attempts=3)
            ),
        },
    )
    tools = resolve_activity_settings(plugin.config.activities)["tools"]
    once = activity_options(tools["exec_command"], effect=True)
    assert once["retry_policy"].maximum_attempts == 1
    assert once["retry_policy"].maximum_interval == timedelta(seconds=5)
    explicit = activity_options(tools["fs_write_file"], effect=True)
    assert explicit["retry_policy"].maximum_attempts == 3


def test_native_retry_policy_keeps_level_attempt_limits(tmp_path, monkeypatch):
    """``activity=ActivityConfig(retry_policy=RetryPolicy(...паузы...))``:
    чтения треда и ресурсы не теряют предел попыток (0 по умолчанию у
    RetryPolicy — «не задано»), модель по-прежнему ждёт без ограничения."""
    from datetime import timedelta

    from gc_core.thread_store_file_durable import DurableFileThreadStore
    from gc_core_temporalio import AgentPlugin
    from gc_core_temporalio.activity_config import (
        activity_options,
        resolve_activity_settings,
    )
    from temporalio.common import RetryPolicy
    from temporalio.workflow import ActivityConfig

    monkeypatch.chdir(tmp_path)
    plugin = AgentPlugin(
        {"a": {}},
        thread_store=DurableFileThreadStore(str(tmp_path / "t")),
        activity=ActivityConfig(
            retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=7))
        ),
    )
    levels = resolve_activity_settings(plugin.config.activities)
    for level in ("resource", "thread_store"):
        policy = activity_options(levels[level])["retry_policy"]
        assert policy.maximum_attempts == 5, level
        assert policy.maximum_interval == timedelta(seconds=7)
    assert (
        activity_options(levels["model"])["retry_policy"].maximum_attempts == 0
    )


async def test_retried_effect_with_changed_guard_facts_is_outcome_unknown(
    monkeypatch,
):
    """Повторная попытка эффекта упёрлась в изменившиеся факты guard: первая
    могла уже всё сделать — модели «исход неизвестен», а не «не исполнялся»."""
    from types import SimpleNamespace

    import gc_core.execution.tool_effects as tool_effects
    import pytest
    from gc_core.execution.contracts import (
        GuardPreconditionChanged,
        ToolOutcomeUnknown,
    )
    from gc_core_temporalio import activities

    async def changed(*_args, **_kwargs):
        raise GuardPreconditionChanged("Guard facts changed after approval")

    monkeypatch.setattr(tool_effects, "execute_native_tool_effect", changed)
    call = SimpleNamespace(data={"effect": True})
    for attempt, expected in (
        (1, GuardPreconditionChanged),
        (2, ToolOutcomeUnknown),
    ):
        monkeypatch.setattr(
            activities.activity,
            "info",
            lambda a=attempt: SimpleNamespace(attempt=a),
        )
        with pytest.raises(expected):
            await activities._effect_attempt(call, None, None)


def test_isolated_model_failure_is_left_to_the_caller_not_to_temporal():
    """Вызов со своим бюджетом повторов (судья действий) повторяет и считает
    сбои сам: его транзиентный сбой уходит в workflow данными, а не
    бесконечным повтором activity. Прочие запросы — как раньше."""
    failure = ModelFailure("provider", "E", "m", {"status_code": 503})
    main = SimpleNamespace(child_context=None)
    child = SimpleNamespace(child_context={"parent": "p"})

    def request(**extra):
        return ModelRequest("model-1", "command_judge", [], {}, **extra)

    assert retried_by_temporal(main, request(), failure) is True
    assert retried_by_temporal(child, request(), failure) is False
    assert (
        retried_by_temporal(main, request(isolated_timeout_sec=60), failure)
        is False
    )


def test_model_request_fields_survive_the_temporal_boundary():
    """Запрос к модели едет в activity словарём через конвертер Temporal и
    собирается обратно `value_to_type` — поля изоляции судьи и немого ответа
    доезжают; история без этих ключей получает умолчания."""
    converter = DataConverter.default.payload_converter

    def across(data):
        (payload,) = converter.to_payloads([data])
        (back,) = converter.from_payloads([payload], [dict])
        return value_to_type(ModelRequest, back)

    request = ModelRequest(
        "model-1",
        "command_judge",
        [{"role": "user", "content": "x"}],
        {},
        isolated_timeout_sec=60,
        mute_answer=True,
    )
    assert across(asdict(request)) == request

    old = asdict(ModelRequest("model-1", "main", [], {}))
    del old["isolated_timeout_sec"], old["mute_answer"]
    restored = across(old)
    assert (restored.isolated_timeout_sec, restored.mute_answer) == (
        None,
        False,
    )
