"""T12 — confirm_port 4-choice matrix (SPEC §3.3).

Y=allow_remember, O=allow_once, N=deny_remember, T=deny_once.
Record matrix: (allow, session) per choice.
Backward-compat: old ids once/session/deny mapped to new ids.
Invalid → deny_remember. allow = choice in (allow_remember, allow_once).
"""

from gc_core.security import confirm_port as cp
from gc_core.security.confirm_port import (
    CONFIRM_ALLOW_ONCE,
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_DENY_ONCE,
    CONFIRM_DENY_REMEMBER,
    request_confirm,
)
from gc_core.security.permission_store import PermissionStore
from gc_testkit.settings import fake_ctx


def _ctx(choice, store=None):
    calls = {"n": 0}

    def tui_confirm(*, title, question, fields, via=None):
        calls["n"] += 1
        return choice

    return fake_ctx(
        is_tui=True,
        tui_confirm=tui_confirm,
        permission_store=store,
    ), calls


# ── constants exist ──────────────────────────────────────────────────────


def test_four_choice_constants_exist():
    assert CONFIRM_ALLOW_REMEMBER == "allow_remember"
    assert CONFIRM_ALLOW_ONCE == "allow_once"
    assert CONFIRM_DENY_REMEMBER == "deny_remember"
    assert CONFIRM_DENY_ONCE == "deny_once"


def test_normalize_confirm_key_matches_tui_sets():
    """Shared key normalizer: Latin/Cyrillic both cases; multi-char → deny_remember."""
    from gc_core.security.confirm_port import normalize_confirm_key

    assert normalize_confirm_key("y") == CONFIRM_ALLOW_REMEMBER
    assert normalize_confirm_key("Н") == CONFIRM_ALLOW_REMEMBER
    assert normalize_confirm_key("щ") == CONFIRM_ALLOW_ONCE
    assert normalize_confirm_key("Т") == CONFIRM_DENY_REMEMBER
    assert normalize_confirm_key("е") == CONFIRM_DENY_ONCE
    assert normalize_confirm_key("yes") == CONFIRM_DENY_REMEMBER
    assert normalize_confirm_key("once") == CONFIRM_DENY_REMEMBER
    assert normalize_confirm_key("s") == CONFIRM_DENY_REMEMBER
    assert normalize_confirm_key("") == CONFIRM_DENY_REMEMBER
    assert normalize_confirm_key(None) == CONFIRM_DENY_REMEMBER


# ── record matrix ────────────────────────────────────────────────────────


def test_y_allows_and_records_session_true():
    store = PermissionStore()
    ctx, calls = _ctx("allow_remember", store)
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:ls",
        )
        is True
    )
    assert store.session_rules() == {"command:ls": True}


def test_o_allows_and_records_run_only():
    store = PermissionStore()
    ctx, calls = _ctx("allow_once", store)
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:ls",
        )
        is True
    )
    assert store.session_rules() == {}
    # run-cache: second call doesn't re-ask
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:ls",
        )
        is True
    )
    assert calls["n"] == 1


def test_n_denies_and_records_session_false():
    store = PermissionStore()
    ctx, calls = _ctx("deny_remember", store)
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:rm",
        )
        is False
    )
    assert store.session_rules() == {"command:rm": False}


def test_t_denies_and_records_run_only():
    store = PermissionStore()
    ctx, calls = _ctx("deny_once", store)
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:rm",
        )
        is False
    )
    assert store.session_rules() == {}
    # run-cache deny: second call doesn't re-ask
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:rm",
        )
        is False
    )
    assert calls["n"] == 1


# ── invalid choice → deny_remember ──────────────────────────────────────


def test_invalid_choice_maps_to_deny_remember():
    store = PermissionStore()
    for bad in ("yes", None, "", "ok", 1, "session_old"):
        ctx, _ = _ctx(bad, store)
        result = request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:x",
        )
        assert result is False, f"bad {bad!r} must deny"
    # invalid → deny_remember → session False
    assert store.session_rules().get("command:x") is False


# ── backward-compat aliases (old tui_confirm returns once/session/deny) ──


def test_alias_once_maps_to_allow_once():
    store = PermissionStore()
    ctx, _ = _ctx("once", store)
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:ls",
        )
        is True
    )
    assert store.session_rules() == {}  # once → allow_once → run only


def test_alias_session_maps_to_allow_remember():
    store = PermissionStore()
    ctx, _ = _ctx("session", store)
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:ls",
        )
        is True
    )
    assert store.session_rules() == {"command:ls": True}


def test_alias_deny_maps_to_deny_remember():
    store = PermissionStore()
    ctx, _ = _ctx("deny", store)
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:rm",
        )
        is False
    )
    assert store.session_rules() == {"command:rm": False}


def test_unavailable_confirm_is_deny_once_not_session(monkeypatch):
    """No confirm channel → deny_once: block without poisoning insession denied."""
    store = PermissionStore()
    monkeypatch.setattr(cp, "confirm_available", lambda ctx: False)
    ctx = fake_ctx(
        is_tui=False,
        permission_store=store,
        tui_confirm=None,
    )
    assert (
        request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="command:curl x",
        )
        is False
    )
    assert store.session_rules() == {}
    assert store.decision_for("command:curl x") is False
    assert store.insession_examples() == {"allowed": [], "denied": []}
