from security.permission_store import PermissionStore


def test_unknown_key_returns_none():
    s = PermissionStore()
    assert s.decision_for("write:/x") is None


def test_record_run_then_decision_for():
    s = PermissionStore()
    s.record("write:/x", True, session=False)
    assert s.decision_for("write:/x") is True
    s.record("write:/y", False, session=False)
    assert s.decision_for("write:/y") is False


def test_session_takes_priority_over_run():
    s = PermissionStore()
    s.record("write:/x", False, session=False)  # run says deny
    s.record("write:/x", True, session=True)    # session says allow
    assert s.decision_for("write:/x") is True


def test_session_record_writes_through_to_passed_dict():
    shared: dict[str, bool] = {}
    s = PermissionStore(session_rules=shared)
    s.record("url:http://a", True, session=True)
    assert shared == {"url:http://a": True}        # by-reference write-through
    s.record("read:/z", True, session=False)
    assert "read:/z" not in shared                 # run-scope не уходит в session


def test_namespace_isolation_is_just_distinct_keys():
    s = PermissionStore()
    s.record("write:/p", True, session=False)
    assert s.decision_for("read:/p") is None       # другой action — другой ключ
