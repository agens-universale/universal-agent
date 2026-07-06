"""read_live_messages: payloads только из live data-файла (без архивов)."""
import json

from tui.services.stores.file_store import FileTaskStore

_TID = "live0000000000000000000000000000a"


def _write(d, name, payloads):
    with open(d / name, "w", encoding="utf-8") as f:
        for i, p in enumerate(payloads, 1):
            f.write(json.dumps({"type": "message", "thread_id": _TID, "system": "cli",
                                "seq": i, "ts": "t", "payload": p}, ensure_ascii=False) + "\n")


def test_reads_live_payloads(tmp_path):
    # архив игнорируется; берём только live
    _write(tmp_path, f"data_cli_{_TID}__pre_summary__20260101_000000.jsonl",
           [{"role": "user", "content": "архив"}])
    _write(tmp_path, f"data_cli_{_TID}.jsonl",
           [{"role": "user", "content": "живой-1"}, {"role": "assistant", "content": "живой-2"}])
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    msgs = store.read_live_messages(_TID)
    assert [m["content"] for m in msgs] == ["живой-1", "живой-2"]


def test_missing_live_file_returns_empty(tmp_path):
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    assert store.read_live_messages(_TID) == []
