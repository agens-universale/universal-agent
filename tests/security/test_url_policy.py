import json
from types import SimpleNamespace

from security.url_policy import (
    enforce_url_policy,
    match_url_blacklist,
    parse_and_validate_url,
    validate_url_policy,
)


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(message % args if args else message)


def test_parse_and_validate_url_requires_scheme_and_host():
    parsed = parse_and_validate_url("HTTPS://Example.COM/path")

    assert parsed.scheme == "https"
    assert parsed.host == "example.com"


def test_validate_url_policy_blocks_disallowed_scheme():
    decision = validate_url_policy("file:///etc/passwd", ["http", "https"], [])

    assert decision.allowed is False
    assert decision.error_type == "url_validation_failed"


def test_validate_url_policy_blocks_blacklist_rules():
    decision = validate_url_policy(
        "https://api.internal.example/v1",
        ["https"],
        ["*.internal.example"],
    )

    assert decision.allowed is False
    assert decision.error_type == "url_blocked_by_policy"
    assert decision.matched_rule == "*.internal.example"


def test_match_url_blacklist_supports_exact_and_glob_rules():
    assert match_url_blacklist("localhost", "https://localhost", ["localhost"]) == "localhost"
    assert match_url_blacklist("api.example.com", "https://api.example.com", ["*.example.com"]) == "*.example.com"
    assert match_url_blacklist("example.com", "https://example.com/private/a", ["*://example.com/private/*"]) == "*://example.com/private/*"


def test_enforce_url_policy_returns_structured_error(tmp_path):
    config_dict = {
        "url_policy": {
            "enabled": True,
            "allowed_schemes": ["https"],
            "url_blacklist": ["blocked.example"],
        }
    }

    @enforce_url_policy(url_arg="url")
    def fetch(url: str, config_dict: dict) -> str:
        return json.dumps({"ok": True, "url": url})

    result = json.loads(fetch("https://blocked.example", config_dict=config_dict))

    assert result["ok"] is False
    assert result["error"]["type"] == "url_blocked_by_policy"
    assert result["error"]["matched_rule"] == "blocked.example"


def test_enforce_url_policy_audits_blocked_decision():
    logger = _Logger()
    ctx = SimpleNamespace(
        config_dict={
            "url_policy": {
                "enabled": True,
                "allowed_schemes": ["https"],
                "url_blacklist": ["blocked.example"],
            }
        },
        logger=logger,
    )

    @enforce_url_policy(url_arg="url")
    def fetch(url: str, **kwargs) -> str:
        return json.dumps({"ok": True, "url": url})

    result = json.loads(fetch("https://blocked.example", ctx=ctx))

    assert result["ok"] is False
    assert '"decision": "blocked"' in logger.messages[-1]
    assert '"reason_code": "policy_deny"' in logger.messages[-1]
    assert '"error_type": "url_blocked_by_policy"' in logger.messages[-1]
