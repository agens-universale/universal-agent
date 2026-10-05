import json

from gc_core.security.url_policy import (
    enforce_url_policy,
    match_url_blacklist,
    parse_and_validate_url,
    validate_url_policy,
)
from gc_testkit.settings import fake_ctx


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
    assert (
        match_url_blacklist("localhost", "https://localhost", ["localhost"])
        == "localhost"
    )
    assert (
        match_url_blacklist(
            "api.example.com", "https://api.example.com", ["*.example.com"]
        )
        == "*.example.com"
    )
    assert (
        match_url_blacklist(
            "example.com",
            "https://example.com/private/a",
            ["*://example.com/private/*"],
        )
        == "*://example.com/private/*"
    )


def test_enforce_url_policy_returns_structured_error(tmp_path):
    ctx = fake_ctx(
        {
            "security.url_policy": {
                "enabled": True,
                "mode": "deny",
                "allowed_schemes": ["https"],
                "url_blacklist": ["blocked.example"],
            }
        }
    )

    @enforce_url_policy(url_arg="url")
    def fetch(url: str, **kwargs) -> str:
        return json.dumps({"ok": True, "url": url})

    result = json.loads(fetch("https://blocked.example", ctx=ctx))

    assert result["ok"] is False
    assert result["error"]["type"] == "url_blocked_by_policy"
    assert result["error"]["matched_rule"] == "blocked.example"


def test_enforce_url_policy_audits_blocked_decision():
    logger = _Logger()
    ctx = fake_ctx(
        {
            "security.url_policy": {
                "enabled": True,
                "mode": "deny",
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


def test_enforce_url_policy_mode_allow_passes_with_audit():
    """mode=allow: blocked URL пропускается, в audit log пишется out_of_perimeter."""
    logger = _Logger()
    ctx = fake_ctx(
        {
            "security.url_policy": {
                "enabled": True,
                "mode": "allow",
                "allowed_schemes": ["https"],
                "url_blacklist": ["blocked.example"],
            }
        },
        logger=logger,
    )

    @enforce_url_policy(url_arg="url")
    def fetch(url: str, **kwargs) -> str:
        return json.dumps({"ok": True, "url": url, "fetched": "data"})

    result = json.loads(fetch("https://blocked.example/x", ctx=ctx))
    assert result["ok"] is True
    assert result["fetched"] == "data"
    assert '"decision": "executed"' in logger.messages[-1]
    assert (
        '"reason_code": "policy_allow_out_of_perimeter"' in logger.messages[-1]
    )
    assert '"risk_flags": ["out_of_perimeter"]' in logger.messages[-1]


def test_enforce_url_policy_mode_ask_confirmed_passes(monkeypatch):
    """mode=ask + confirm → URL пропускается + audit policy_ask_confirmed."""
    from gc_core.security import confirm_port as _cp

    monkeypatch.setattr(_cp, "confirm_available", lambda ctx: True)
    monkeypatch.setattr(_cp, "request_confirm", lambda **_: True)

    logger = _Logger()
    ctx = fake_ctx(
        {
            "security.url_policy": {
                "enabled": True,
                "mode": "ask",
                "allowed_schemes": ["https"],
                "url_blacklist": ["blocked.example"],
            }
        },
        logger=logger,
    )

    @enforce_url_policy(url_arg="url")
    def fetch(url: str, **kwargs) -> str:
        return json.dumps({"ok": True, "url": url})

    result = json.loads(fetch("https://blocked.example/x", ctx=ctx))
    assert result["ok"] is True
    assert '"reason_code": "policy_ask_confirmed"' in logger.messages[-1]


def test_enforce_url_policy_mode_ask_declined_blocks(monkeypatch):
    """mode=ask + decline → блок, error.type = url_blocked_by_policy_cancelled_by_user."""
    from gc_core.security import confirm_port as _cp

    monkeypatch.setattr(_cp, "confirm_available", lambda ctx: True)
    monkeypatch.setattr(_cp, "request_confirm", lambda **_: False)

    logger = _Logger()
    ctx = fake_ctx(
        {
            "security.url_policy": {
                "enabled": True,
                "mode": "ask",
                "allowed_schemes": ["https"],
                "url_blacklist": ["blocked.example"],
            }
        },
        logger=logger,
    )

    @enforce_url_policy(url_arg="url")
    def fetch(url: str, **kwargs) -> str:
        return json.dumps({"ok": True, "url": url})

    result = json.loads(fetch("https://blocked.example/x", ctx=ctx))
    assert result["ok"] is False
    assert result["error"]["type"] == "url_blocked_by_policy_cancelled_by_user"
    assert '"reason_code": "policy_ask_cancelled"' in logger.messages[-1]
