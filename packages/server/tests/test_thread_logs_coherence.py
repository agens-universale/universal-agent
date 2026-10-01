import pytest
from gc_core import thread_store_journal as journal
from gc_core.thread_store_durable import ThreadStoreOperation
from gc_core.thread_store_file import FileThreadStore
from gc_server import server


def _prepared(tmp_path, monkeypatch):
    original = journal._recover

    def interrupt(directory, base):
        if (base / "pending.json").exists():
            raise OSError("interrupted before canonical files")
        original(directory, base)

    monkeypatch.setattr(journal, "_recover", interrupt)
    with pytest.raises(OSError):
        FileThreadStore(str(tmp_path)).commit_operation(
            "a_b",
            "c_d",
            ThreadStoreOperation(
                "persist",
                "persist",
                {
                    "messages": [{"id": "message"}],
                    "meta": {"messages_count": 1},
                },
            ),
        )
    monkeypatch.setattr(journal, "_recover", original)
    monkeypatch.setattr(server, "resolve_threads_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(server, "temporal_registry", None)


@pytest.mark.asyncio
async def test_logs_recover_consistent_messages_and_meta(
    tmp_path, monkeypatch
):
    _prepared(tmp_path, monkeypatch)
    result = await server.get_thread_logs("a_b", "c_d")
    assert result["meta"]["messages_count"] == len(result["messages"]) == 1


@pytest.mark.asyncio
async def test_all_logs_recovers_new_thread_before_enumeration(
    tmp_path, monkeypatch
):
    _prepared(tmp_path, monkeypatch)
    (tmp_path / "meta_a_b_c_d__pre_summary__old.json").write_text("{}")
    result = await server.get_all_thread_logs()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["system"] == "a_b"
    assert result["logs"][0]["task_id"] == "c_d"
