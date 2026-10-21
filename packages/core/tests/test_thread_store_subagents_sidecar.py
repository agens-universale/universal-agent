"""Трасса субагентов треда (поток ``subagent_events``): на диске — файл
develop ``data_subagents_<system>_<thread>.jsonl`` с плоскими записями и
монотонным ``seq``; читается тем же ``read_subagent_trace``, удаляется вместе
с тредом."""

import asyncio
import json
from pathlib import Path

from gc_core.thread_store import (
    append_subagent_record,
    subagent_trace_path,
)
from gc_testkit.develop_files import read_subagent_trace
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey

KEY = ThreadKey(system="cli", thread="t1")


def _append(threads_dir, kind, payload, key=KEY):
    return asyncio.run(
        append_subagent_record(
            FileThreadStore(threads_dir),
            key,
            parent_call_id="call_sub_1",
            child_run_id="run_c1",
            kind=kind,
            payload=payload,
        )
    )


def test_subagent_trace_path_shape(tmp_path: Path) -> None:
    path = subagent_trace_path(str(tmp_path), "CLI", "task-1")
    assert path == str(tmp_path / "data_subagents_cli_task-1.jsonl")


def test_append_assigns_monotonic_seq_and_flat_envelope(
    tmp_path: Path,
) -> None:
    r1 = _append(
        tmp_path,
        "call",
        {
            "type": "function_call",
            "name": "shell",
            "arguments": '{"cmd":"ls"}',
            "call_id": "call_child_9",
        },
    )
    r2 = _append(
        tmp_path,
        "result",
        {
            "type": "function_call_output",
            "call_id": "call_child_9",
            "output": "a.txt\n",
        },
    )

    assert r1["seq"] == 1
    assert r2["seq"] == 2
    assert set(r1) == {
        "ts",
        "seq",
        "parent_call_id",
        "child_run_id",
        "kind",
        "payload",
    }
    path = Path(subagent_trace_path(str(tmp_path), "cli", "t1"))
    lines = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["seq"] for row in lines] == [1, 2]
    assert lines[0]["kind"] == "call"
    assert lines[1]["payload"]["output"] == "a.txt\n"


def test_read_subagent_trace_returns_records_in_file_order(
    tmp_path: Path,
) -> None:
    _append(tmp_path, "call", {"type": "function_call", "call_id": "x"})
    _append(tmp_path, "finished", {"ok": True, "steps": 1})

    records = read_subagent_trace(str(tmp_path), "cli", "t1")
    assert [r["kind"] for r in records] == ["call", "finished"]
    assert records[1]["payload"]["ok"] is True


def test_read_subagent_trace_missing_is_empty(tmp_path: Path) -> None:
    assert read_subagent_trace(str(tmp_path), "cli", "missing") == []


def test_delete_removes_subagents_sidecar(tmp_path: Path) -> None:
    other = ThreadKey(system="cli", thread="other")
    _append(tmp_path, "call", {"type": "function_call", "call_id": "x"})
    _append(tmp_path, "call", {"type": "function_call", "call_id": "y"}, other)
    asyncio.run(FileThreadStore(tmp_path).delete(KEY, op_id="delete"))
    assert not Path(subagent_trace_path(str(tmp_path), "cli", "t1")).exists()
    assert Path(subagent_trace_path(str(tmp_path), "cli", "other")).exists()


def test_recreated_thread_trace_starts_over(tmp_path: Path) -> None:
    _append(tmp_path, "call", {"type": "function_call", "call_id": "x"})
    asyncio.run(FileThreadStore(tmp_path).delete(KEY, op_id="delete"))
    again = _append(
        tmp_path, "call", {"type": "function_call", "call_id": "z"}
    )
    assert again["seq"] == 1
