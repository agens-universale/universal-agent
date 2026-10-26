"""Per-field детект pending_field_keys для визарда."""

from gc_core import onboarding
from gc_testkit.settings import make_settings


def test_stock_defaults_pending_endpoint_and_token_not_model():
    cfg = {
        "backend.completions_url": onboarding.URL_SENTINEL,
        "backend.chat_completion_headers": {
            "Authorization": onboarding.TOKEN_SENTINEL
        },
        "agent.model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.pending_field_keys(make_settings(cfg)) == {
        "completions_url",
        "Authorization",
    }


def test_all_set_returns_empty():
    cfg = {
        "backend.completions_url": "https://x/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "agent.model": "qwen3-coder",
    }
    assert onboarding.pending_field_keys(make_settings(cfg)) == set()


def test_agent_auth_covers_token():
    cfg = {
        "backend.completions_url": "https://x/v1",
        "backend.chat_completion_headers": {
            "Authorization": onboarding.TOKEN_SENTINEL
        },
        "agent.model": "m",
        "agent_auth": {"auth_endpoint": "https://auth"},
    }
    assert "Authorization" not in onboarding.pending_field_keys(
        make_settings(cfg)
    )
