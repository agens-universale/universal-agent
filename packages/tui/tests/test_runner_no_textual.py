"""main() без textual должен дать понятный stderr и exit 1, без трейсбэка."""

import importlib.util


def test_main_without_textual_returns_1_and_prints_to_stderr(
    monkeypatch, capsys
):
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "textual":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    from gc_tui import main

    rc = main(argv=[])

    err = capsys.readouterr().err
    assert rc == 1
    assert "textual" in err.lower()
    assert "uv tool install" in err.lower()
    assert err.count("\n") <= 1
