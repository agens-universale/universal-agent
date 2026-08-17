"""Тесты для src/swarm/storage.py.

Покрывает meta/messages/stdout/stderr и новую функцию load_completion_info
(добавлена в шаге M1/1.2 — см. ~/swarm-lab/specs/swarm1_plan.md).

Все операции — на tmp_path; никаких реальных вызовов.
"""

import json
import os
import sys

import pytest


from gc_server.swarm import storage as storage


# ---------------------------------------------------------------------------
# load_swarm_meta / save_swarm_meta
# ---------------------------------------------------------------------------


def test_load_swarm_meta_missing_returns_empty(tmp_path):
    assert storage.load_swarm_meta(str(tmp_path), "no_such_id") == {}


def test_save_then_load_meta_roundtrip(tmp_path):
    storage.save_swarm_meta(
        str(tmp_path),
        "sys_t1",
        base_meta={"system": "sys", "task_id": "t1"},
        swarm_meta_patch={"id": "sys_t1", "status": "queued", "pid": 12345},
    )
    meta = storage.load_swarm_meta(str(tmp_path), "sys_t1")
    assert meta["system"] == "sys"
    assert meta["task_id"] == "t1"
    assert meta["swarm_meta"]["status"] == "queued"
    assert meta["swarm_meta"]["pid"] == 12345
    assert "created_at" in meta
    assert "updated_at" in meta


def test_save_meta_merges_swarm_meta_patch(tmp_path):
    """Повторный save должен merge'ить patch в существующий swarm_meta, не затирая поля."""
    storage.save_swarm_meta(
        str(tmp_path),
        "x_y",
        base_meta={"system": "x", "task_id": "y"},
        swarm_meta_patch={"id": "x_y", "status": "queued", "pid": 100, "limits": {"timeout_seconds": 60}},
    )
    storage.save_swarm_meta(
        str(tmp_path),
        "x_y",
        base_meta={"system": "x", "task_id": "y"},
        swarm_meta_patch={"status": "running", "started_at": "2026-06-04T00:00:00Z"},
    )
    meta = storage.load_swarm_meta(str(tmp_path), "x_y")
    sm = meta["swarm_meta"]
    assert sm["status"] == "running"
    assert sm["pid"] == 100  # сохранён из первого save
    assert sm["limits"] == {"timeout_seconds": 60}  # тоже сохранён
    assert sm["started_at"] == "2026-06-04T00:00:00Z"  # добавился из второго


def test_load_meta_corrupted_json_returns_empty(tmp_path):
    base = tmp_path / "abc"
    base.mkdir()
    (base / "meta.json").write_text("{not valid")
    assert storage.load_swarm_meta(str(tmp_path), "abc") == {}


def test_save_meta_atomic_replaces_tmp(tmp_path):
    """После save файл .tmp не должен оставаться, только meta.json."""
    storage.save_swarm_meta(
        str(tmp_path), "atom",
        base_meta={"system": "s", "task_id": "t"},
        swarm_meta_patch={"id": "atom", "status": "queued"},
    )
    files = sorted(p.name for p in (tmp_path / "atom").iterdir())
    assert "meta.json" in files
    assert "meta.json.tmp" not in files


# ---------------------------------------------------------------------------
# load_completion_info — новая функция шага 1.2
# ---------------------------------------------------------------------------


def test_load_completion_info_missing_returns_empty(tmp_path):
    assert storage.load_completion_info(str(tmp_path), "no_such") == {}


def test_load_completion_info_valid_json(tmp_path):
    base = tmp_path / "sid"
    base.mkdir()
    payload = {
        "exit_code": 0,
        "finished_at": "2026-06-04T10:00:00Z",
        "error_type": "",
        "error_message": "",
    }
    (base / "completion.json").write_text(json.dumps(payload))
    result = storage.load_completion_info(str(tmp_path), "sid")
    assert result == payload


def test_load_completion_info_corrupted_returns_empty(tmp_path):
    base = tmp_path / "bad"
    base.mkdir()
    (base / "completion.json").write_text("not json {")
    assert storage.load_completion_info(str(tmp_path), "bad") == {}


def test_load_completion_info_non_dict_returns_empty(tmp_path):
    """JSON parsed как list — не dict — обработать gracefully."""
    base = tmp_path / "list"
    base.mkdir()
    (base / "completion.json").write_text("[1, 2, 3]")
    assert storage.load_completion_info(str(tmp_path), "list") == {}


def test_load_completion_info_in_exports():
    assert "load_completion_info" in storage.__all__


# ---------------------------------------------------------------------------
# messages.jsonl
# ---------------------------------------------------------------------------


def test_append_then_load_messages_roundtrip(tmp_path):
    storage.append_swarm_message(str(tmp_path), "mid", stream="stdout", message="hello")
    storage.append_swarm_message(str(tmp_path), "mid", stream="stderr", message="warn")
    msgs = storage.load_swarm_messages(str(tmp_path), "mid")
    assert len(msgs) == 2
    assert msgs[0]["stream"] == "stdout"
    assert msgs[0]["message"] == "hello"
    assert msgs[1]["stream"] == "stderr"


def test_load_messages_skips_corrupted_lines(tmp_path):
    base = tmp_path / "skip"
    base.mkdir()
    (base / "messages.jsonl").write_text(
        '{"ts":"x","stream":"stdout","message":"ok"}\n'
        "garbage line\n"
        '{"ts":"y","stream":"stdout","message":"ok2"}\n'
        "\n"
    )
    msgs = storage.load_swarm_messages(str(tmp_path), "skip")
    assert len(msgs) == 2
    assert msgs[0]["message"] == "ok"
    assert msgs[1]["message"] == "ok2"


def test_load_messages_missing_returns_empty(tmp_path):
    assert storage.load_swarm_messages(str(tmp_path), "none") == []


# ---------------------------------------------------------------------------
# Канал E — messages.jsonl из sandbox-каталога (M4/1.9, AR-2)
# ---------------------------------------------------------------------------


def test_resolve_messages_path_legacy_when_no_sandbox(tmp_path):
    """Без sandbox_root_dir — fallback на legacy путь."""
    result = storage._resolve_messages_path(str(tmp_path / "threads"), "sid")
    assert result == str(tmp_path / "threads" / "sid" / "messages.jsonl")


def test_resolve_messages_path_legacy_when_sandbox_file_missing(tmp_path):
    """Sandbox задан, но файла там нет — fallback на legacy путь."""
    threads = tmp_path / "th"; threads.mkdir()
    sandbox = tmp_path / "sb"; sandbox.mkdir()
    result = storage._resolve_messages_path(str(threads), "sid", str(sandbox))
    assert result == str(threads / "sid" / "messages.jsonl")


def test_resolve_messages_path_prefers_sandbox_when_exists(tmp_path):
    """Если в sandbox есть файл — используем его (это и есть канал E)."""
    threads = tmp_path / "th"; threads.mkdir()
    sandbox = tmp_path / "sb"; sandbox.mkdir()
    # Создаём канал-E путь в sandbox.
    sandbox_path = sandbox / "sid" / ".gc" / "threads" / "sid"
    sandbox_path.mkdir(parents=True)
    (sandbox_path / "messages.jsonl").write_text('{"ts":"x","stream":"y","message":"z"}\n')

    result = storage._resolve_messages_path(str(threads), "sid", str(sandbox))
    assert result == str(sandbox_path / "messages.jsonl")


def test_load_swarm_messages_reads_from_sandbox(tmp_path):
    """Полный round-trip: канал-E messages.jsonl читается через load_swarm_messages."""
    threads = tmp_path / "th"; threads.mkdir()
    sandbox = tmp_path / "sb"; sandbox.mkdir()
    sandbox_path = sandbox / "sid" / ".gc" / "threads" / "sid"
    sandbox_path.mkdir(parents=True)
    (sandbox_path / "messages.jsonl").write_text(
        '{"ts":"2026-06-04T10:00:00Z","stream":"stdout","message":"hello"}\n'
    )

    msgs = storage.load_swarm_messages(str(threads), "sid", str(sandbox))
    assert len(msgs) == 1
    assert msgs[0]["message"] == "hello"


def test_load_swarm_messages_legacy_signature_still_works(tmp_path):
    """Старая сигнатура без sandbox_root_dir остаётся работоспособной."""
    storage.append_swarm_message(str(tmp_path), "sid", stream="stdout", message="ok")
    msgs = storage.load_swarm_messages(str(tmp_path), "sid")
    assert len(msgs) == 1


# ---------------------------------------------------------------------------
# stdout/stderr
# ---------------------------------------------------------------------------


def test_load_stdout_stderr_missing_returns_empty_tuple(tmp_path):
    out, err = storage.load_swarm_stdout_stderr(str(tmp_path), "missing")
    assert out == []
    assert err == []


def test_load_stdout_stderr_returns_lines(tmp_path):
    base = tmp_path / "sid"
    base.mkdir()
    (base / "stdout.log").write_text("line1\nline2\n")
    (base / "stderr.log").write_text("err1\n")
    out, err = storage.load_swarm_stdout_stderr(str(tmp_path), "sid")
    assert out == ["line1", "line2"]
    assert err == ["err1"]
