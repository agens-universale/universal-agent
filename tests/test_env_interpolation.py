import logging
import os as _os

import pytest

from config_modules.env_interpolation import resolve_env_only, resolve_env_placeholders


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
    if _os.name == "posix":
        _os.chmod(f, 0o600)
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


def test_file_marker_size_cap(tmp_path, caplog):
    f = tmp_path / "big"
    f.write_bytes(b"a" * (65536 + 1))
    with caplog.at_level(logging.WARNING):
        assert resolve_env_placeholders(f"${{FILE:{f}}}") == ""
    assert any(str(f) in r.getMessage() for r in caplog.records)


def test_file_marker_size_cap_boundary_ok(tmp_path):
    f = tmp_path / "max"
    f.write_bytes(b"a" * 65536)
    assert resolve_env_placeholders(f"${{FILE:{f}}}") == "a" * 65536


@pytest.mark.skipif(_os.name != "posix", reason="POSIX file modes only")
def test_file_marker_lax_permissions_warn(tmp_path, caplog):
    f = tmp_path / "tok"
    f.write_text("v\n")
    _os.chmod(f, 0o644)
    with caplog.at_level(logging.WARNING):
        assert resolve_env_placeholders(f"${{FILE:{f}}}") == "v"
    assert any(
        "accessible" in r.getMessage() and str(f) in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.skipif(_os.name != "posix", reason="POSIX file modes only")
def test_file_marker_strict_permissions_no_warn(tmp_path, caplog):
    f = tmp_path / "tok"
    f.write_text("v\n")
    _os.chmod(f, 0o600)
    with caplog.at_level(logging.WARNING):
        assert resolve_env_placeholders(f"${{FILE:{f}}}") == "v"
    assert not any(str(f) in r.getMessage() for r in caplog.records)


def test_file_marker_same_path_read_once(tmp_path, monkeypatch):
    f = tmp_path / "tok"
    f.write_text("v")
    import builtins
    real_open = builtins.open
    opens: list[str] = []

    def counting_open(file, *args, **kwargs):
        if str(file) == str(f):
            opens.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    out = resolve_env_placeholders({"a": f"${{FILE:{f}}}", "b": f"${{FILE:{f}}}"})
    assert out == {"a": "v", "b": "v"}
    assert len(opens) == 1


def test_file_path_can_come_from_env(tmp_path, monkeypatch):
    """ENV резолвится раньше FILE: путь к файлу можно собрать из переменной."""
    f = tmp_path / "tok"
    f.write_text("combo\n")
    if _os.name == "posix":
        _os.chmod(f, 0o600)
    monkeypatch.setenv("SECRET_DIR", str(tmp_path))
    assert resolve_env_placeholders("${FILE:${ENV:SECRET_DIR}/tok}") == "combo"


# --- resolve_env_only (только ${ENV:...}, для мест без права на файловый I/O) --

def test_env_only_replaces_env_marker(monkeypatch):
    monkeypatch.setenv("MY_TOK2", "secret123")
    assert resolve_env_only("${ENV:MY_TOK2}") == "secret123"


def test_env_only_leaves_file_marker_as_a_literal_reference(tmp_path):
    f = tmp_path / "tok"
    f.write_text("file-secret")
    assert resolve_env_only(f"${{FILE:{f}}}") == f"${{FILE:{f}}}"


def test_env_only_never_touches_the_filesystem(tmp_path, monkeypatch):
    """Не просто «оставляет ссылку как текст» — файловый резолвер вообще не
    задет: чистая функция для мест, где резолвить можно только на UI-потоке
    (`tui/editors/model_entry_form.py::_as_validator_sees`) и файловое чтение
    подвесило бы интерфейс."""
    import config_modules.env_interpolation as env_interp

    calls: list = []
    monkeypatch.setattr(env_interp, "_read_secret_file",
                        lambda *a, **k: (calls.append(a), "")[1])
    f = tmp_path / "tok"
    f.write_text("v")
    resolve_env_only(f"${{FILE:{f}}}")
    assert calls == []


def test_env_only_missing_var_empty_and_silent(monkeypatch, caplog):
    """Без логирования вовсе (не только без дедупа) — вызывающая сторона
    (`_pair_env_missing`) уже называет причину текстом в самой форме, и
    WARNING на каждый набранный символ был бы шумом, а не сигналом."""
    monkeypatch.delenv("NOPE2", raising=False)
    with caplog.at_level(logging.WARNING):
        assert resolve_env_only("${ENV:NOPE2}") == ""
    assert not any("NOPE2" in r.getMessage() for r in caplog.records)


def test_env_only_nested_structures(monkeypatch):
    monkeypatch.setenv("T3", "tok")
    out = resolve_env_only({"a": ["${ENV:T3}", 5], "b": {"c": "${ENV:T3}"}, "n": True})
    assert out == {"a": ["tok", 5], "b": {"c": "tok"}, "n": True}


def test_env_only_none_passthrough():
    assert resolve_env_only(None) is None
