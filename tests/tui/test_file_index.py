"""Юнит-тесты file_index: парс @-сегмента, listing, fuzzy, резолв, футер."""
from tui.services.file_index import parse_at_segment


def test_at_at_start():
    assert parse_at_segment("@age", 4) == (0, "age")

def test_at_after_space_midphrase():
    text = "почини @age"
    assert parse_at_segment(text, len(text)) == (7, "age")

def test_at_inside_word_not_triggered():
    assert parse_at_segment("foo@bar", 7) is None

def test_space_in_segment_breaks():
    text = "@agents now"
    assert parse_at_segment(text, len(text)) is None

def test_cursor_before_at():
    assert parse_at_segment("x @age", 1) is None

def test_path_with_slash_is_segment():
    assert parse_at_segment("@agents/core", 12) == (0, "agents/core")

def test_trailing_slash_kept():
    assert parse_at_segment("@agents/", 8) == (0, "agents/")


from tui.services.file_index import list_dir


def test_list_dir_dirs_before_files_with_slash(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    out = list_dir(str(tmp_path), "")
    assert out == ["agents/", "zeta/", ".env", "alpha.py"]

def test_list_dir_subdir(tmp_path):
    (tmp_path / "agents" / "core").mkdir(parents=True)
    (tmp_path / "agents" / "a.py").write_text("x", encoding="utf-8")
    out = list_dir(str(tmp_path), "agents")
    assert out == ["agents/core/", "agents/a.py"]

def test_list_dir_hides_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    assert list_dir(str(tmp_path), "") == ["a.py"]


import subprocess

from tui.services.file_index import fuzzy_search, rank


def test_rank_prefix_of_basename_first():
    paths = ["src/agent_runner.py", "agents/agent.py", "x/zagent.py"]
    out = rank("agent", paths)
    assert out[0] in ("agents/agent.py",)

def test_fuzzy_non_git_fallback_walk(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("x", encoding="utf-8")
    out = fuzzy_search(str(tmp_path), "agent")
    assert "agents/agent.py" in out
    assert all(".venv" not in p for p in out)

def test_fuzzy_git_respects_gitignore(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / "secret.log").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    out = fuzzy_search(str(tmp_path), "")
    assert "agents/" in out
    out2 = fuzzy_search(str(tmp_path), "log")
    assert all(not p.endswith("secret.log") for p in out2)


import os

from tui.services.file_index import (
    extract_at_mentions, resolve_attachment, build_attachment_footer,
)


def test_extract_mentions():
    text = "сравни @a.py и @sub/b.py, но не foo@bar"
    assert extract_at_mentions(text) == ["a.py", "sub/b.py"]

def test_resolve_file_and_dir(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    f = resolve_attachment(str(tmp_path), "a.py")
    d = resolve_attachment(str(tmp_path), "sub/")
    assert f[0] == "a.py" and f[2] == "файл" and os.path.isabs(f[1])
    assert d[0] == "sub/" and d[2] == "папка" and os.path.isabs(d[1])
    assert resolve_attachment(str(tmp_path), "nope.py") is None

def test_build_footer():
    body = build_attachment_footer([
        ("a.py", r"D:\proj\a.py", "файл"),
        ("sub/", r"D:\proj\sub", "папка"),
    ])
    assert "@a.py" in body and r"D:\proj\a.py" in body and "(файл)" in body
    assert "@sub/" in body and "(папка)" in body
    assert body.splitlines()[0].startswith("Пользователь прикрепил через «@»")

def test_build_footer_empty():
    assert build_attachment_footer([]) == ""


def test_fuzzy_git_dir_respects_gitignore(tmp_path):
    """В git-репо .gitignore'нутая ПАПКА не протекает в fuzzy (деривация из ls-files)."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "run.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("logs/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    out = fuzzy_search(str(tmp_path), "log")
    # папка logs/ проигнорена git'ом → не появляется
    assert all("logs" not in p for p in out)
    # tracked-папка agents/ доступна
    out2 = fuzzy_search(str(tmp_path), "agent")
    assert "agents/" in out2
