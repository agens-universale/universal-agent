"""Таймауты и повторы activities настраиваются по уровням, как у Pydantic AI."""

import pytest
from gc_core.execution.contracts import ModelFailure
from gc_ua_temporalio.native_activities import is_transient_provider_failure
from gc_ua_temporalio.native_activity_config import (
    NON_RETRYABLE_ERROR_TYPES,
    ActivitiesConfig,
    activity_options,
    resolve_activity_settings,
)
from gc_ua_temporalio.native_provider import NativeWorkerConfig
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
    options = activity_options(settings["resource"], effect=True)
    assert (
        options["retry_policy"].maximum_attempts == 2
    )  # эффект — не настраивается


def test_activity_config_is_validated_on_worker_start(tmp_path):
    with pytest.raises(ValidationError):
        NativeWorkerConfig.model_validate(
            {
                "receipts_database": str(tmp_path / "r.sqlite"),
                "profiles": {},
                "activities": {"model": {"retry": {"maximum_atempts": 3}}},
            }
        )


@pytest.mark.parametrize(
    ("details", "flags", "transient"),
    [
        ({}, {}, True),  # обрыв транспорта
        ({"status_code": 503}, {}, True),
        ({"status_code": 429}, {}, True),
        ({"status_code": 400}, {}, False),
        ({"status_code": 401}, {"auth_error": True}, False),
        ({"status_code": 400}, {"context_overflow": True}, False),
        ({"status_code": None, "raw_code": "insufficient_quota"}, {}, False),
        ({"alias": "a", "model": "m", "reason": "open"}, {}, False),
    ],
)
def test_transient_provider_failures(details, flags, transient):
    failure = ModelFailure("provider", "E", "m", details, **flags)
    assert is_transient_provider_failure(failure) is transient
