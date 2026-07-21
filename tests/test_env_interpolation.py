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


# --- ${FILE:} ---

def test_file_marker_reads_and_strips_trailing_newline(tmp_path):
    f = tmp_path / "tok"
    f.write_text("secret-value\n")
    assert resolve_env_placeholders(f"${{FILE:{f}}}") == "secret-value"


def test_file_marker_keeps_inner_newlines_and_spaces(tmp_path):
    f = tmp_path / "pem"
    f.write_text("line1\nline2 \n\n")
    assert resolve_env_placeholders(f"${{FILE:{f}}}") == "line1\nline2 "


def test_file_marker_composition(tmp_path):
    f = tmp_path / "tok"
    f.write_text("abc123\n")
    assert resolve_env_placeholders(f"Bearer ${{FILE:{f}}}") == "Bearer abc123"


def test_file_marker_in_nested_structures(tmp_path):
    f = tmp_path / "tok"
    f.write_text("v")
    out = resolve_env_placeholders({"a": [f"${{FILE:{f}}}", 5], "b": {"c": f"${{FILE:{f}}}"}})
    assert out == {"a": ["v", 5], "b": {"c": "v"}}


def test_file_marker_missing_empty_and_warns_once(tmp_path, caplog):
    missing = tmp_path / "nope"
    with caplog.at_level(logging.WARNING):
        out = resolve_env_placeholders({"a": f"${{FILE:{missing}}}", "b": f"x ${{FILE:{missing}}} y"})
    assert out == {"a": "", "b": "x  y"}
    warns = [r for r in caplog.records if str(missing) in r.getMessage()]
    assert len(warns) == 1  # dedup по пути


def test_file_marker_warning_has_no_content(tmp_path, caplog):
    f = tmp_path / "bin"
    f.write_bytes(b"\xff\xfe\x00secret")  # не-UTF-8 → ошибка чтения
    with caplog.at_level(logging.WARNING):
        assert resolve_env_placeholders(f"${{FILE:{f}}}") == ""
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "secret" not in joined
    assert str(f) in joined


def test_file_marker_empty_file_no_warning(tmp_path, caplog):
    f = tmp_path / "empty"
    f.write_text("")
    with caplog.at_level(logging.WARNING):
        assert resolve_env_placeholders(f"${{FILE:{f}}}") == ""
    assert not any(str(f) in r.getMessage() for r in caplog.records)


def test_file_marker_expanduser(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "tok"
    f.write_text("home-secret\n")
    assert resolve_env_placeholders("${FILE:~/tok}") == "home-secret"


def test_env_marker_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("T", "envval")
    f = tmp_path / "tok"
    f.write_text("fileval")
    out = resolve_env_placeholders(f"${{ENV:T}}/${{FILE:{f}}}")
    assert out == "envval/fileval"
