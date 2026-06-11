"""main() без textual должен дать понятный stderr и exit 1, без трейсбэка."""
import builtins
import io
import sys

import pytest


def test_main_without_textual_returns_1_and_prints_to_stderr(monkeypatch, capsys):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "textual" or name.startswith("textual."):
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Remove cached tui.app
    for mod in list(sys.modules):
        if mod.startswith("tui.app") or mod == "tui.app":
            sys.modules.pop(mod, None)

    from tui import main
    rc = main(argv=[])

    err = capsys.readouterr().err
    assert rc == 1
    assert "textual" in err.lower()
    assert "pip install" in err.lower()
    assert err.count("\n") <= 1
