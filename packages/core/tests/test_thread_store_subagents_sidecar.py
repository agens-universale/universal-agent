"""Store API для sidecar nested-трейса: data_subagents_{system}_{task_id}.jsonl."""

import json
from pathlib import Path

from gc_core.thread_store import append_subagent_trace_record, delete_thread, read_subagent_trace, reset_thread_files, subagent_trace_path


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

    deleted = reset_thread_files(
        str(threads_dir), system_id="cli", thread_id="demo",
        launch_root=str(tmp_path),
    )

    assert str(sidecar) in deleted
    assert not sidecar.exists()
    assert not data.exists()
    assert other.exists()


# ---------------------------------------------------------------------------
# O(1) append: process-local seq counter + lock (SPEC 2026-07-30-sidecar-append-o1-seq)
# ---------------------------------------------------------------------------
import threading

from gc_core import thread_store as _ts


def _append_call(threads_dir: str, seq_hint: int = 1) -> object:
    return append_subagent_trace_record(
        threads_dir,
        "cli",
        "t1",
        parent_call_id="p1",
        child_run_id="c1",
        kind="call",
        payload={"type": "function_call", "call_id": f"x{seq_hint}", "name": "shell"},
    )


def test_append_does_not_rescan_after_cold_init(monkeypatch, tmp_path):
    """T2: cold init scan ровно один раз на path; далее O(1) без rescan."""
    threads_dir = str(tmp_path)
    calls = {"n": 0}
    real = _ts._max_subagent_trace_seq

    def spy(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(_ts, "_max_subagent_trace_seq", spy)

    r1 = _append_call(threads_dir, 1)
    r2 = _append_call(threads_dir, 2)
    r3 = _append_call(threads_dir, 3)

    assert [r1["seq"], r2["seq"], r3["seq"]] == [1, 2, 3]
    assert calls["n"] == 1, "rescan должен происходить ровно один раз (cold init)"


def test_append_cold_init_continues_from_preseed_seq(tmp_path):
    """T3: preseed файла с seq 1..K → первый append даёт K+1 (cold init scan)."""
    threads_dir = str(tmp_path)
    path = tmp_path / "data_subagents_cli_t1.jsonl"
    preseed = []
    import json as _json
    import datetime as _dt

    for seq in range(1, 6):  # K = 5
        preseed.append(
            _json.dumps(
                {
                    "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "seq": seq,
                    "parent_call_id": "p1",
                    "child_run_id": "c1",
                    "kind": "call",
                    "payload": {"type": "function_call", "call_id": f"old{seq}"},
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(preseed) + "\n", encoding="utf-8")

    r = _append_call(threads_dir, 99)
    assert r["seq"] == 6


def test_delete_thread_resets_seq_counter(tmp_path):
    """T4: delete_thread сбрасывает счётчик → следующий append снова seq=1."""
    threads_dir = str(tmp_path)
    (tmp_path / "locks").mkdir()
    r1 = _append_call(threads_dir, 1)
    assert r1["seq"] == 1
    r2 = _append_call(threads_dir, 2)
    assert r2["seq"] == 2

    delete_thread(threads_dir, "cli", "t1")

    r3 = _append_call(threads_dir, 3)
    assert r3["seq"] == 1, "после delete счётчик должен сброситься"


def test_reset_thread_files_resets_seq_counter(tmp_path):
    """T4: reset_thread_files сбрасывает счётчик → следующий append снова seq=1."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    td = str(threads_dir)
    r1 = _append_call(td, 1)
    assert r1["seq"] == 1
    r2 = _append_call(td, 2)
    assert r2["seq"] == 2

    reset_thread_files(td, system_id="cli", thread_id="t1", launch_root=str(tmp_path))

    r3 = _append_call(td, 3)
    assert r3["seq"] == 1, "после reset счётчик должен сброситься"


def test_parallel_writers_unique_monotonic_seq(tmp_path):
    """T5: N параллельных writers на один path → уникальные монотонные seq без дырок/дублей."""
    threads_dir = str(tmp_path)
    n_writers = 8
    per_writer = 25
    barrier = threading.Barrier(n_writers)
    errors: list = []

    def writer(wid: int) -> None:
        try:
            barrier.wait()
            for i in range(per_writer):
                append_subagent_trace_record(
                    threads_dir,
                    "cli",
                    "t1",
                    parent_call_id=f"p{wid}",
                    child_run_id=f"c{wid}",
                    kind="call",
                    payload={
                        "type": "function_call",
                        "call_id": f"w{wid}_{i}",
                        "name": "shell",
                    },
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"errors in writers: {errors}"

    records = read_subagent_trace(threads_dir, "cli", "t1")
    seqs = [r["seq"] for r in records]
    assert len(seqs) == n_writers * per_writer
    assert seqs == list(range(1, n_writers * per_writer + 1)), (
        "seq должен быть плотной монотонной последовательностью 1..N без дублей и дырок"
    )
