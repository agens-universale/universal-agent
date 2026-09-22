"""Юнит-тесты tui.services.file_view: resolve, probe, window, store, limits."""
from pathlib import Path

from gc_tui.services.file_view import (
    DEFAULT_MAX_FILE_BYTES,
    MSG_BINARY,
    MSG_IS_DIR,
    MSG_NOT_FOUND,
    MSG_TOO_LARGE,
    MSG_USAGE,
    FileViewSettings,
    OpenErr,
    OpenOk,
    ViewPositionStore,
    clamp_start_line,
    count_lines,
    format_range_label,
    is_probably_binary,
    lexer_name_for_path,
    open_for_view,
    read_window,
    resolve_view_path,
    textarea_language_for_lexer,
    view_window_renderable,
)
from rich.syntax import Syntax


def test_resolve_relative_uses_cwd(tmp_path):
    p = resolve_view_path("a.txt", cwd=str(tmp_path))
    assert p == tmp_path / "a.txt"


def test_resolve_absolute_ignores_cwd(tmp_path):
    target = tmp_path / "x.txt"
    p = resolve_view_path(str(target), cwd="/other")
    assert p == target


def test_resolve_expanduser(monkeypatch, tmp_path):
    # Path.expanduser() на Windows читает USERPROFILE, на POSIX — HOME: подменяем оба.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = resolve_view_path("~/f.txt", cwd="/tmp")
    assert p == tmp_path / "f.txt"


def test_open_usage_empty():
    r = open_for_view("", cwd=".")
    assert isinstance(r, OpenErr)
    assert r.message == MSG_USAGE


def test_open_not_found(tmp_path):
    r = open_for_view("missing.txt", cwd=str(tmp_path))
    assert isinstance(r, OpenErr)
    assert MSG_NOT_FOUND in r.message


def test_open_directory(tmp_path):
    r = open_for_view(".", cwd=str(tmp_path))
    assert isinstance(r, OpenErr)
    assert r.message == MSG_IS_DIR


def test_open_ok_small_file_counts_lines(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    r = open_for_view("a.txt", cwd=str(tmp_path))
    assert isinstance(r, OpenOk)
    assert r.total_lines == 3
    assert r.path == f.resolve()


def test_open_binary(tmp_path):
    f = tmp_path / "b.bin"
    f.write_bytes(b"\x00" * 100)
    r = open_for_view("b.bin", cwd=str(tmp_path))
    assert isinstance(r, OpenErr)
    assert r.message == MSG_BINARY
    assert is_probably_binary(f)


def test_open_too_large(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x", encoding="utf-8")
    settings = FileViewSettings(max_file_bytes=1)
    # make size > 1
    f.write_text("hello", encoding="utf-8")
    r = open_for_view("big.txt", cwd=str(tmp_path), settings=settings)
    assert isinstance(r, OpenErr)
    assert MSG_TOO_LARGE in r.message


def test_open_disabled(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("ok", encoding="utf-8")
    r = open_for_view("a.txt", cwd=str(tmp_path), settings=FileViewSettings(enabled=False))
    assert isinstance(r, OpenErr)
    assert "выключен" in r.message


def test_read_window_1based(tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
    w = read_window(f, start_line=3, line_count=3)
    assert w.lines == ("L3", "L4", "L5")
    assert w.start_line == 3
    assert w.end_line == 5


def test_read_window_past_eof(tmp_path):
    f = tmp_path / "n.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    w = read_window(f, start_line=10, line_count=5)
    assert w.lines == ()


def test_read_window_normalizes_crlf(tmp_path):
    f = tmp_path / "w.txt"
    f.write_bytes(b"a\r\nb\r\n")
    w = read_window(f, start_line=1, line_count=10)
    assert w.lines == ("a", "b")


def test_count_lines(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    assert count_lines(f) == 3


def test_clamp_start_line():
    assert clamp_start_line(100, total_lines=50, page=10) == 41
    assert clamp_start_line(1, total_lines=50, page=10) == 1
    assert clamp_start_line(5, total_lines=0, page=10) == 1


def test_position_store_roundtrip(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("x", encoding="utf-8")
    store = ViewPositionStore()
    assert store.get(f) is None
    store.set(f, 42)
    assert store.get(f) == 42
    assert store.get(f.resolve()) == 42


def test_format_range_label():
    assert format_range_label(start_line=1, end_line=10, total_lines=100) == "1–10 / 100"
    assert format_range_label(start_line=1, end_line=10, total_lines=None) == "1–10 / ?"
    assert format_range_label(
        start_line=1, end_line=0, total_lines=0, empty=True,
    ) == "— / 0"


def test_settings_from_mapping():
    s = FileViewSettings.from_mapping({
        "enabled": False, "max_file_bytes": 1024, "page_lines": 40,
    })
    assert s.enabled is False
    assert s.max_file_bytes == 1024
    assert s.page_lines == 40
    assert FileViewSettings.from_mapping(None).max_file_bytes == DEFAULT_MAX_FILE_BYTES
    assert FileViewSettings.from_mapping(None).page_lines is None


def test_settings_bad_max_bytes_falls_back():
    assert FileViewSettings.from_mapping({"max_file_bytes": "nope"}).max_file_bytes == (
        DEFAULT_MAX_FILE_BYTES
    )


def test_settings_page_lines_auto_and_clamp():
    assert FileViewSettings.from_mapping({"page_lines": None}).page_lines is None
    assert FileViewSettings.from_mapping({}).page_lines is None
    assert FileViewSettings.from_mapping({"page_lines": 0}).page_lines is None
    assert FileViewSettings.from_mapping({"page_lines": "x"}).page_lines is None
    assert FileViewSettings.from_mapping({"page_lines": 999}).page_lines == 200
    assert FileViewSettings.from_mapping({"page_lines": 10}).page_lines == 10


def test_lexer_name_for_known_and_plain():
    assert lexer_name_for_path(Path("x.py")) == "python"
    assert lexer_name_for_path(Path("a.yml")) in {"yaml", "yml"}
    assert lexer_name_for_path(Path("notes.txt")) is None
    assert lexer_name_for_path(Path("noext")) is None


def test_textarea_language_for_lexer_maps_aliases():
    assert textarea_language_for_lexer("python") == "python"
    assert textarea_language_for_lexer("js") == "javascript"
    assert textarea_language_for_lexer("typescript") == "javascript"
    assert textarea_language_for_lexer("tsql") == "sql"
    assert textarea_language_for_lexer("c") is None
    assert textarea_language_for_lexer(None) is None


def test_pygments_highlight_map_python_keywords():
    from gc_tui.services.file_view import pygments_highlight_map

    mapped = pygments_highlight_map("def hello():\n    return 1\n", lexer="python")
    styles = {name for spans in mapped.values() for _, _, name in spans}
    assert "keyword" in styles
    assert mapped  # непусто


def test_view_window_renderable_syntax_or_plain():
    plain = view_window_renderable("a = 1", lexer=None)
    assert plain == "a = 1"
    syn = view_window_renderable("a = 1", lexer="python")
    assert isinstance(syn, Syntax)
    assert "python" in syn.lexer.aliases
