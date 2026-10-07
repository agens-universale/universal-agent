"""Таймауты и повторы activities настраиваются по уровням, как у Pydantic AI."""

import socket
import ssl

import httpx
import pytest
from gc_core.execution.contracts import ModelFailure
from gc_core_temporalio.model_error_encoding import encode_model_failure
from gc_core_temporalio.activities import is_transient_provider_failure
from gc_core_temporalio.activity_config import (
    NON_RETRYABLE_ERROR_TYPES,
    ActivitiesConfig,
    activity_options,
    resolve_activity_settings,
)
from gc_core_temporalio.provider import WorkerConfig
from pydantic import ValidationError


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
