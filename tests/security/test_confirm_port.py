import types
from security.confirm_port import confirm_available, request_confirm


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
        return True
    ctx = _ctx(is_tui=True, tui_confirm=banner)
    assert confirm_available(ctx) is True
    assert request_confirm(title="t", question="q?", fields=[("k", "v")], ctx=ctx) is True
    assert answered["question"] == "q?"


def test_cli_disabled_when_ask_human_in_stop_tools(monkeypatch):
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: True))
    ctx = _ctx(config_dict={"stop_tools": ["ask_human"]})
    assert confirm_available(ctx) is False
