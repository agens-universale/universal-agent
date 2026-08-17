import os
import threading
from pathlib import Path

import pytest

from gc_core.project_memory import storage
from gc_core.project_memory.storage import ProjectMemoryError, ProjectMemoryStat


def test_stat_missing_returns_zero(tmp_path):
    res = storage.stat(tmp_path / "project_memory.md")
    assert res == ProjectMemoryStat(exists=False, chars=0, lines=0)


def test_stat_existing_counts_chars_and_lines(tmp_path):
    p = tmp_path / "project_memory.md"
    text = "line one\nline two\nline three\n"
    p.write_text(text, encoding="utf-8")
    res = storage.stat(p)
    assert res.exists is True
    assert res.chars == len(text)
    assert res.lines == text.count("\n") + (0 if text.endswith("\n") else 1)


def test_read_missing_returns_empty_string(tmp_path):
    assert storage.read(tmp_path / "project_memory.md") == ""


def test_read_existing_returns_full_content(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("hello", encoding="utf-8")
    assert storage.read(p) == "hello"


def test_write_creates_file_and_parent_dirs(tmp_path):
    p = tmp_path / ".gc" / "project_memory.md"
    res = storage.write(p, "hello world")
    assert p.is_file()
    assert p.read_text(encoding="utf-8") == "hello world"
    assert res.exists is True
    assert res.chars == len("hello world")


def test_write_strips_whitespace(tmp_path):
    p = tmp_path / "project_memory.md"
    storage.write(p, "  hello\n\n  ")
    assert p.read_text(encoding="utf-8") == "hello"


def test_write_atomic_no_tmp_leftover(tmp_path):
    p = tmp_path / "project_memory.md"
    storage.write(p, "abc")
    leftovers = [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]
    assert leftovers == []


def test_write_empty_raises_empty_content(tmp_path):
    p = tmp_path / "project_memory.md"
    with pytest.raises(ProjectMemoryError) as ei:
        storage.write(p, "   \n  ")
    assert ei.value.code == "empty_content"
    assert not p.exists()


def test_write_oversize_raises_limit_exceeded_file_unchanged(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("original", encoding="utf-8")
    huge = "x" * (storage.MAX_CHARS + 1)
    with pytest.raises(ProjectMemoryError) as ei:
        storage.write(p, huge)
    assert ei.value.code == "limit_exceeded"
    assert p.read_text(encoding="utf-8") == "original"


def test_append_to_missing_file_creates_it(tmp_path):
    p = tmp_path / "project_memory.md"
    storage.append(p, "first entry")
    assert p.read_text(encoding="utf-8") == "first entry"


def test_append_concatenates_with_blank_line(tmp_path):
    p = tmp_path / "project_memory.md"
    storage.append(p, "first")
    storage.append(p, "second")
    assert p.read_text(encoding="utf-8") == "first\n\nsecond"


def test_append_strips_each_part(tmp_path):
    p = tmp_path / "project_memory.md"
    storage.append(p, "  one  ")
    storage.append(p, "\n\ntwo\n\n")
    assert p.read_text(encoding="utf-8") == "one\n\ntwo"


def test_append_empty_raises_empty_content(tmp_path):
    p = tmp_path / "project_memory.md"
    with pytest.raises(ProjectMemoryError) as ei:
        storage.append(p, "   ")
    assert ei.value.code == "empty_content"


def test_append_oversize_raises_limit_exceeded_file_unchanged(tmp_path):
    p = tmp_path / "project_memory.md"
    p.write_text("seed", encoding="utf-8")
    huge = "x" * storage.MAX_CHARS  # seed + \n\n + huge > MAX
    with pytest.raises(ProjectMemoryError) as ei:
        storage.append(p, huge)
    assert ei.value.code == "limit_exceeded"
    assert p.read_text(encoding="utf-8") == "seed"


def test_concurrent_append_does_not_lose_data(tmp_path):
    """Two threads each appending N times — final file must contain all 2N deltas."""
    p = tmp_path / "project_memory.md"
    n_per_thread = 50
    barrier = threading.Barrier(2)

    def worker(prefix: str) -> None:
        barrier.wait()
        for i in range(n_per_thread):
            storage.append(p, f"{prefix}-{i}")

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final = p.read_text(encoding="utf-8")
    for prefix in ("A", "B"):
        for i in range(n_per_thread):
            assert f"{prefix}-{i}" in final, f"missing {prefix}-{i} in final file"
