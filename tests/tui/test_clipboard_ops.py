"""clipboard_ops.copy_out — единый write-path (OSC + native; bool = native)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gc_tui.services import clipboard_ops


def _app(*, notify=None):
    calls = {"osc": [], "notify": []}

    def copy_to_clipboard(text: str) -> None:
        calls["osc"].append(text)

    def _notify(msg: str, **kw) -> None:
        calls["notify"].append((msg, kw))
        if notify is not None:
            notify(msg, **kw)

    return SimpleNamespace(
        copy_to_clipboard=copy_to_clipboard,
        notify=_notify,
        _clipboard_warn_shown=False,
        _calls=calls,
    )


def test_copy_out_calls_osc_and_native_returns_native(monkeypatch):
    app = _app()
    seen = {}

    def fake_write(text: str) -> bool:
        seen["text"] = text
        return True

    monkeypatch.setattr(clipboard_ops, "write_clipboard", fake_write)
    assert clipboard_ops.copy_out(app, "hello", toast=True) is True
    assert app._calls["osc"] == ["hello"]
    assert seen["text"] == "hello"


def test_copy_out_returns_false_when_native_fails(monkeypatch):
    app = _app()
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: False)
    assert clipboard_ops.copy_out(app, "x", toast=False) is False
    assert app._calls["osc"] == ["x"]  # OSC всё равно вызван


def test_copy_out_empty_is_noop(monkeypatch):
    app = _app()
    called = {"n": 0}
    monkeypatch.setattr(
        clipboard_ops, "write_clipboard", lambda t: called.__setitem__("n", 1) or True
    )
    assert clipboard_ops.copy_out(app, "", toast=True) is False
    assert app._calls["osc"] == []
    assert called["n"] == 0


def test_toast_only_on_hotkey_success(monkeypatch):
    app = _app()
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: True)
    clipboard_ops.copy_out(app, "a", toast=True)
    assert app._calls["notify"] == [
        (clipboard_ops.MSG_COPIED, {"severity": "information"})
    ]
    app2 = _app()
    clipboard_ops.copy_out(app2, "a", toast=False)
    assert app2._calls["notify"] == []  # copy-on-select — тихий успех


def test_native_fail_linux_local_warn_once(monkeypatch):
    app = _app()
    monkeypatch.setattr(clipboard_ops.sys, "platform", "linux")
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: False)
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: False)
    clipboard_ops.copy_out(app, "a", toast=True)
    clipboard_ops.copy_out(app, "b", toast=False)
    assert len(app._calls["notify"]) == 1
    assert clipboard_ops.MSG_LINUX_DEPS in app._calls["notify"][0][0]
    assert app._calls["notify"][0][1]["severity"] == "warning"


def test_native_fail_ssh_hint_not_package(monkeypatch):
    app = _app()
    monkeypatch.setattr(clipboard_ops.sys, "platform", "linux")
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: True)
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: False)
    clipboard_ops.copy_out(app, "a", toast=False)
    assert len(app._calls["notify"]) == 1
    assert clipboard_ops.MSG_SSH_OSC in app._calls["notify"][0][0]
    assert "xclip" not in app._calls["notify"][0][0].lower()


def test_native_fail_macos_silent(monkeypatch):
    app = _app()
    monkeypatch.setattr(clipboard_ops.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: False)
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: False)
    clipboard_ops.copy_out(app, "a", toast=True)
    assert app._calls["notify"] == []  # не package-hint


def test_copy_out_async_schedules_worker(monkeypatch):
    app = _app()
    workers: list = []
    written: list = []

    def fake_worker(fn, **kw):
        workers.append(kw.get("name"))
        fn()  # sync for test
        return None

    app.run_worker = fake_worker
    app.call_from_thread = lambda fn, *a, **k: fn(*a, **k)
    monkeypatch.setattr(
        clipboard_ops, "write_clipboard", lambda t: written.append(t) or True
    )
    assert clipboard_ops.copy_out(app, "hello", toast=True, blocking=False) is True
    assert app._calls["osc"] == ["hello"]
    assert written == ["hello"]
    assert workers == ["clipboard-write"]
    assert app._calls["notify"] == [
        (clipboard_ops.MSG_COPIED, {"severity": "information"})
    ]


def test_is_ssh_session_env(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    assert clipboard_ops.is_ssh_session() is False
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    assert clipboard_ops.is_ssh_session() is True


def test_with_source_cite_file_and_diff():
    assert clipboard_ops.with_source_cite("code", kind="file", path="a.py") == (
        "code\n[file: a.py]"
    )
    assert clipboard_ops.with_source_cite("+x\n", kind="diff", path="b.py") == (
        "+x\n[diff: b.py]"
    )


def test_with_source_cite_empty_noop():
    assert clipboard_ops.with_source_cite("", kind="file", path="a.py") == ""
    assert clipboard_ops.with_source_cite("code", kind="file", path="") == "code"
    assert clipboard_ops.with_source_cite("code", kind="file", path="  ") == "code"
