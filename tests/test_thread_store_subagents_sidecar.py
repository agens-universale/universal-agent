"""Store API для sidecar nested-трейса: data_subagents_{system}_{task_id}.jsonl."""

import json
from pathlib import Path

from thread_store import (
    append_subagent_trace_record,
    delete_thread,
    read_subagent_trace,
    reset_thread_files,
    subagent_trace_path,
)


def test_subagent_trace_path_shape(tmp_path: Path) -> None:
    path = subagent_trace_path(str(tmp_path), "CLI", "task-1")
    assert path == str(tmp_path / "data_subagents_cli_task-1.jsonl")


def test_append_assigns_monotonic_seq_and_flat_envelope(tmp_path: Path) -> None:
    threads_dir = str(tmp_path)
    r1 = append_subagent_trace_record(
        threads_dir,
        "cli",
        "t1",
        parent_call_id="call_sub_1",
        child_run_id="run_c1",
        kind="call",
        payload={
            "type": "function_call",
            "name": "shell",
            "arguments": '{"cmd":"ls"}',
            "call_id": "call_child_9",
        },
    )
    r2 = append_subagent_trace_record(
        threads_dir,
        "cli",
        "t1",
        parent_call_id="call_sub_1",
        child_run_id="run_c1",
        kind="result",
        payload={
            "type": "function_call_output",
            "call_id": "call_child_9",
            "output": "a.txt\n",
        },
    )

    assert r1["seq"] == 1
    assert r2["seq"] == 2
    assert set(r1) == {"ts", "seq", "parent_call_id", "child_run_id", "kind", "payload"}
    assert "type" not in r1
    assert "thread_id" not in r1
    assert "system" not in r1

    path = Path(subagent_trace_path(threads_dir, "cli", "t1"))
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in lines] == [1, 2]
    assert lines[0]["kind"] == "call"
    assert lines[1]["payload"]["output"] == "a.txt\n"


def test_read_subagent_trace_returns_records_in_file_order(tmp_path: Path) -> None:
    threads_dir = str(tmp_path)
    append_subagent_trace_record(
        threads_dir,
        "cli",
        "t1",
        parent_call_id="p1",
        child_run_id="c1",
        kind="call",
        payload={"type": "function_call", "call_id": "x"},
    )
    append_subagent_trace_record(
        threads_dir,
        "cli",
        "t1",
        parent_call_id="p1",
        child_run_id="c1",
        kind="finished",
        payload={"ok": True, "steps": 1},
    )

    records = read_subagent_trace(threads_dir, "cli", "t1")
    assert [r["kind"] for r in records] == ["call", "finished"]
    assert records[1]["payload"]["ok"] is True


def test_read_subagent_trace_missing_is_empty(tmp_path: Path) -> None:
    assert read_subagent_trace(str(tmp_path), "cli", "missing") == []


def test_delete_thread_removes_subagents_sidecar(tmp_path: Path) -> None:
    threads_dir = str(tmp_path)
    (tmp_path / "locks").mkdir()
    sidecar = tmp_path / "data_subagents_cli_abc.jsonl"
    data = tmp_path / "data_cli_abc.jsonl"
    meta = tmp_path / "meta_cli_abc.json"
    other = tmp_path / "data_subagents_cli_other.jsonl"
    for p in (sidecar, data, meta, other):
        p.write_text("x\n", encoding="utf-8")

    deleted = delete_thread(threads_dir, "cli", "abc")

    assert str(sidecar) in deleted
    assert not sidecar.exists()
    assert other.exists()


def test_reset_thread_files_removes_subagents_sidecar(tmp_path: Path) -> None:
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    sidecar = threads_dir / "data_subagents_cli_demo.jsonl"
    data = threads_dir / "data_cli_demo.jsonl"
    other = threads_dir / "data_subagents_cli_other.jsonl"
    sidecar.write_text("x\n", encoding="utf-8")
    data.write_text("y\n", encoding="utf-8")
    other.write_text("z\n", encoding="utf-8")

    deleted = reset_thread_files(str(threads_dir), system_id="cli", thread_id="demo")

    assert str(sidecar) in deleted
    assert not sidecar.exists()
    assert not data.exists()
    assert other.exists()
