"""T1-T7 — insession_examples() + judge-safe store invariant (SPEC §3.2 / §6).

Caps: last 12 allowed, 8 denied; 280 chars + …; strip "command:" prefix.
Only session_rules contribute (run_rules never).
T6: judge verdict=safe never writes store / few-shot.
"""

import json
from types import SimpleNamespace

from gc_core.security.command_moderation import moderate_command_execution
from gc_core.security.permission_store import PermissionStore


def _populate(store, rules):
    for key, val in rules.items():
        store.record(key, val, session=True)
    return store


def test_t1_empty_store_returns_empty():
    s = PermissionStore()
    ex = s.insession_examples()
    assert ex == {"allowed": [], "denied": []}


def test_t2_y_command_in_allowed_session_true():
    s = PermissionStore()
    s.record("command:ls -la", True, session=True)
    ex = s.insession_examples()
    assert ex["allowed"] == ["ls -la"]
    assert ex["denied"] == []


def test_t3_o_allow_once_not_in_allowed():
    s = PermissionStore()
    s.record("command:ls -la", True, session=False)  # run only (O)
    ex = s.insession_examples()
    assert ex["allowed"] == []
    assert ex["denied"] == []


def test_t4_n_command_in_denied_session_false():
    s = PermissionStore()
    s.record("command:rm -rf /", False, session=True)
    ex = s.insession_examples()
    assert ex["denied"] == ["rm -rf /"]
    assert ex["allowed"] == []


def test_t5_t_deny_once_not_in_denied():
    s = PermissionStore()
    s.record("command:rm -rf /", False, session=False)  # run only (T)
    ex = s.insession_examples()
    assert ex["denied"] == []
    assert ex["allowed"] == []


def test_t6_judge_safe_does_not_write_store(tmp_path, monkeypatch):
    """SPEC §6 T6: verdict=safe → exec without confirm; store / insession unchanged."""
    from gc_core.security import command_moderation as cm
    from gc_core.security import confirm_port as cp

    store = PermissionStore()
    ctx = SimpleNamespace(
        config_dict={
            "launch_root": str(tmp_path),
            "command_moderation": {"enabled": True, "mode": "ask"},
        },
        permission_store=store,
        is_tui=True,
        tui_confirm=lambda **_kw: (_ for _ in ()).throw(
            AssertionError("confirm must not run on judge-safe")
        ),
    )

    def boom_confirm(*_a, **_k):
        raise AssertionError("request_confirm must not run on judge-safe")

    def boom_ask(*_a, **_k):
        raise AssertionError(
            "_ask_user_confirmation must not run on judge-safe"
        )

    monkeypatch.setattr(cp, "request_confirm", boom_confirm)
    monkeypatch.setattr(cm, "request_confirm", boom_confirm)
    monkeypatch.setattr(cm, "_ask_user_confirmation", boom_ask)

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command",
        judge_client_extractor=lambda *_a, **_k: (
            lambda _payload: {
                "verdict": "safe",
                "reasons": ["read-only pytest"],
                "risk_flags": [],
                "confidence": 0.95,
            }
        ),
    )
    def run(command: str, **kwargs) -> str:
        return json.dumps({"ok": True, "command": command})

    payload = "pytest tests/unit"
    result = json.loads(run(payload, ctx=ctx))
    assert result["ok"] is True
    assert result["command"] == payload
    assert store.session_rules() == {}
    assert store.decision_for(f"command:{payload}") is None
    assert store.insession_examples() == {"allowed": [], "denied": []}


def test_t6b_non_command_keys_excluded():
    s = PermissionStore()
    s.record("write:/tmp/x", True, session=True)
    s.record("url:http://a", False, session=True)
    s.record("command:echo hi", True, session=True)
    ex = s.insession_examples()
    assert ex["allowed"] == ["echo hi"]
    assert ex["denied"] == []


def test_t7_cap_12_allowed_8_denied():
    s = PermissionStore()
    for i in range(15):
        s.record(f"command:allowed_cmd_{i}", True, session=True)
    for i in range(12):
        s.record(f"command:denied_cmd_{i}", False, session=True)
    ex = s.insession_examples()
    assert len(ex["allowed"]) == 12
    assert len(ex["denied"]) == 8
    # Last 12 / 8 by insertion order (dict preserves order)
    assert ex["allowed"][0] == "allowed_cmd_3"
    assert ex["denied"][0] == "denied_cmd_4"


def test_t7b_truncate_280_chars():
    long_cmd = "x" * 300
    s = PermissionStore()
    s.record(f"command:{long_cmd}", True, session=True)
    ex = s.insession_examples()
    val = ex["allowed"][0]
    assert len(val) <= 281  # 280 + …
    assert val.endswith("…")
    assert len(val) == 281


def test_t7c_command_prefix_stripped():
    s = PermissionStore()
    s.record("command:curl http://x", True, session=True)
    ex = s.insession_examples()
    assert ex["allowed"] == ["curl http://x"]
    # key still has prefix in store
    assert "command:curl http://x" in s.session_rules()
