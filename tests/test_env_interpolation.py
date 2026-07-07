import logging

from config_modules.env_interpolation import resolve_env_placeholders


def test_replaces_marker(monkeypatch):
    monkeypatch.setenv("MY_TOK", "secret123")
    assert resolve_env_placeholders("${ENV:MY_TOK}") == "secret123"


def test_embedded_and_multiple(monkeypatch):
    monkeypatch.setenv("K", "abc")
    monkeypatch.setenv("V", "xyz")
    assert resolve_env_placeholders("Bearer ${ENV:K}/${ENV:V}") == "Bearer abc/xyz"


def test_nested_structures(monkeypatch):
    monkeypatch.setenv("T", "tok")
    out = resolve_env_placeholders({"a": ["${ENV:T}", 5], "b": {"c": "${ENV:T}"}, "n": True})
    assert out == {"a": ["tok", 5], "b": {"c": "tok"}, "n": True}


def test_non_mutating(monkeypatch):
    monkeypatch.setenv("T", "tok")
    src = {"x": "${ENV:T}"}
    resolve_env_placeholders(src)
    assert src["x"] == "${ENV:T}"


def test_empty_env_var_honored_no_warning(monkeypatch, caplog):
    monkeypatch.setenv("EMPTY", "")
    with caplog.at_level(logging.WARNING):
        assert resolve_env_placeholders("${ENV:EMPTY}") == ""
    assert not any("EMPTY" in r.message for r in caplog.records)


def test_missing_var_empty_and_warns(monkeypatch, caplog):
    monkeypatch.delenv("NOPE", raising=False)
    with caplog.at_level(logging.WARNING):
        out = resolve_env_placeholders({"a": "${ENV:NOPE}", "b": "x ${ENV:NOPE} y"})
    assert out == {"a": "", "b": "x  y"}
    warns = [r for r in caplog.records if "NOPE" in r.message]
    assert len(warns) == 1  # deduped
