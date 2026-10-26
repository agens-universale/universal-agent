import pytest
from gc_core.config_modules.logging_config import (
    LoggingConfig,
    RedactSecretsConfig,
)
from pydantic import ValidationError


def test_defaults_preserve_main():
    lc = LoggingConfig()
    assert lc.format == "text"
    assert lc.log_level == "info"
    assert lc.redact_secrets.enabled is False


def test_format_validator():
    assert LoggingConfig(format="JSONL").format == "jsonl"
    with pytest.raises(ValidationError):
        LoggingConfig(format="xml")


def test_log_level_validator():
    assert LoggingConfig(log_level="DEBUG").log_level == "debug"
    with pytest.raises(ValidationError):
        LoggingConfig(log_level="trace")


def test_redact_secrets_shape():
    rc = RedactSecretsConfig(
        enabled=True, extra_keys=["sid"], extra_patterns=["X-[0-9]+"]
    )
    assert rc.enabled is True and rc.extra_keys == ["sid"]
    with pytest.raises(ValidationError):
        RedactSecretsConfig(enabled=True, bogus=1)


def test_load_settings_reads_logging_keys(tmp_path, monkeypatch):
    import yaml
    from gc_core.config import load_settings

    p = tmp_path / "config.yml"
    p.write_text(
        yaml.safe_dump(
            {
                "logging": {
                    "format": "jsonl",
                    "log_level": "debug",
                    "redact_secrets": {"enabled": True},
                },
            }
        ),
        encoding="utf-8",
    )
    logging = load_settings(str(p)).settings.logging
    assert logging.format == "jsonl"
    assert logging.log_level == "debug"
    assert logging.redact_secrets.enabled is True
