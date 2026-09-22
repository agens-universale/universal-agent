"""TouchedJournal: per-trace path journal (spec 2026-07-21)."""
from __future__ import annotations

import json
from pathlib import Path

from gc_core.thread_store import touched_journal_path
from gc_tui.diff_tui.touched import TouchedJournal


def test_path_helper_matches_data_meta_suffix(tmp_path):
    threads = str(tmp_path / "threads")
    path = touched_journal_path(threads, "Tui", "abc-1")
    assert path == str(Path(threads) / "touched_tui_abc-1.json")


def test_add_discard_paths_roundtrip(tmp_path):
    j = TouchedJournal(str(tmp_path), "tui", "t1")
    assert j.paths() == set()
    j.add("src/a.py")
    j.add("src/b.py")
    j.add("src/a.py")  # idempotent
    assert j.paths() == {"src/a.py", "src/b.py"}
    raw = json.loads(Path(j.path).read_text(encoding="utf-8"))
    assert "ts" in raw["paths"]["src/a.py"]
    j.discard("src/a.py")
    assert j.paths() == {"src/b.py"}
    j.discard_many(["src/b.py", "missing"])
    assert j.paths() == set()


def test_missing_file_is_empty(tmp_path):
    j = TouchedJournal(str(tmp_path), "tui", "none")
    assert j.paths() == set()
    assert not Path(j.path).exists()


def test_corrupt_json_is_empty(tmp_path):
    j = TouchedJournal(str(tmp_path), "tui", "bad")
    Path(j.path).parent.mkdir(parents=True, exist_ok=True)
    Path(j.path).write_text("{not-json", encoding="utf-8")
    assert j.paths() == set()
    j.add("x.py")
    assert j.paths() == {"x.py"}


def test_write_failure_is_soft(tmp_path, monkeypatch):
    j = TouchedJournal(str(tmp_path), "tui", "soft")
    j.add("ok.py")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("gc_tui.diff_tui.touched.atomic_replace", boom)
    j.add("fail.py")  # must not raise
    assert "ok.py" in j.paths()


def test_sessions_are_isolated(tmp_path):
    a = TouchedJournal(str(tmp_path), "tui", "A")
    b = TouchedJournal(str(tmp_path), "tui", "B")
    a.add("only_a.py")
    b.add("only_b.py")
    assert a.paths() == {"only_a.py"}
    assert b.paths() == {"only_b.py"}
