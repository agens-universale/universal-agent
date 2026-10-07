import pytest

from gc_core.config_modules.model_registry import model_authorization_header
from gc_core.utils.http_auth import authorization_attempts


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ("", []),
        (" key ", ["key", "Bearer key"]),
        ("Bearer key", ["Bearer key", "key"]),
        ("bearer key", ["bearer key", "key"]),
        ("Basic abc", ["Basic abc"]),
        ("Custom abc", ["Custom abc"]),
    ],
)
def test_authorization_candidates(raw, expected):
    assert authorization_attempts(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "key", "Bearer key", "Basic abc", "Custom abc"])
def test_model_header_preserves_configured_form(raw):
    assert model_authorization_header(raw) == raw
