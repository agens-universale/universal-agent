import types
from security import confirm_port as cp
from security.confirm_port import confirm_available, request_confirm
from security.permission_store import PermissionStore


def _ctx(**kw):
    c = types.SimpleNamespace(is_tui=False, config_dict={}, tui_confirm=None)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_unavailable_in_headless_denies(monkeypatch):
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))
    ctx = _ctx()
    assert confirm_available(ctx) is False
    assert request_confirm(title="t", question="q?", fields=[], ctx=ctx) is False


def test_tui_always_available_and_routes_to_banner():
    answered = {}
    def banner(**kw):
        answered.update(kw)
        return "once"
    ctx = _ctx(is_tui=True, tui_confirm=banner)
    assert confirm_available(ctx) is True
    assert request_confirm(title="t", question="q?", fields=[("k", "v")], ctx=ctx) is True
    assert answered["question"] == "q?"


def test_cli_disabled_when_ask_human_in_stop_tools(monkeypatch):
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: True))
    ctx = _ctx(config_dict={"stop_tools": ["turn_ask_human"]})
    assert confirm_available(ctx) is False


def _tui_ctx(choice, store=None):
    # is_tui ctx с tui_confirm, возвращающим заданный choice; счётчик вызовов.
    calls = {"n": 0}
    def tui_confirm(*, title, question, fields):
        calls["n"] += 1
        return choice
    ctx = types.SimpleNamespace(
        is_tui=True, tui_confirm=tui_confirm, config_dict={}, permission_store=store,
    )
    return ctx, calls


def test_choice_once_allows_and_records_run_only():
    store = PermissionStore()
    ctx, calls = _tui_ctx("once", store)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is True
    assert calls["n"] == 1
    assert store.session_rules() == {}                 # once не в session
    # повтор того же ключа — без нового вопроса (run-кэш)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is True
    assert calls["n"] == 1


def test_choice_session_allows_and_records_session():
    store = PermissionStore()
    ctx, calls = _tui_ctx("session", store)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is True
    assert store.session_rules() == {"write:/x": True}


def test_choice_deny_blocks_and_caches_run():
    store = PermissionStore()
    ctx, calls = _tui_ctx("deny", store)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is False
    # повтор — без нового вопроса, всё ещё deny (anti-loop)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is False
    assert calls["n"] == 1


def test_no_decision_key_always_asks():
    store = PermissionStore()
    ctx, calls = _tui_ctx("once", store)
    cp.request_confirm(title="t", question="q", fields=[], ctx=ctx)
    cp.request_confirm(title="t", question="q", fields=[], ctx=ctx)
    assert calls["n"] == 2                              # без ключа — кэш не работает


def test_headless_fail_closed_without_tty(monkeypatch):
    monkeypatch.setattr(cp.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    ctx = types.SimpleNamespace(config_dict={}, permission_store=PermissionStore())
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is False


def test_invalid_tui_confirm_return_maps_to_deny():
    # tui_confirm, возвращающий невалидное значение, должен приводить к deny (не allow).
    for bad_choice in ("yes", None, "", "ok", 1):
        ctx, _ = _tui_ctx(bad_choice)
        result = cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x")
        assert result is False, f"bad choice {bad_choice!r} должен давать deny, но вернул {result}"
