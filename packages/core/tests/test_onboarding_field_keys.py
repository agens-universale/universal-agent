"""Per-field детект pending_field_keys для визарда."""

from gc_core import onboarding
from gc_testkit.settings import make_settings


def test_stock_defaults_pending_endpoint_and_token_not_model():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {
            "Authorization": onboarding.TOKEN_SENTINEL
        },
        "model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.pending_field_keys(make_settings(cfg)) == {
        "completions_url",
        "Authorization",
    }


def test_all_set_returns_empty():
    cfg = {
        "completions_url": "https://x/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "model": "qwen3-coder",
    }
    assert onboarding.pending_field_keys(make_settings(cfg)) == set()


def test_agent_auth_covers_token():
    cfg = {
        "completions_url": "https://x/v1",
        "chat_completion_headers": {
            "Authorization": onboarding.TOKEN_SENTINEL
        },
        "model": "m",
        "agent_auth": {"auth_endpoint": "https://auth"},
    }
    assert "Authorization" not in onboarding.pending_field_keys(
        make_settings(cfg)
    )
