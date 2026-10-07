"""Файлы acp-sessions и отбор тредов system=acp."""

import json

from gc_core.thread_store import append_new_thread_messages

from gc_acp.session_index import SessionIndex


def _clock(values):
    stream = iter(values)
    return lambda: next(stream)


def _thread(root, session_id, messages):
    append_new_thread_messages(str(root), "acp", session_id, messages, set())


def test_list_orders_by_updated_at_and_cuts_title(tmp_path):
    index = SessionIndex(
        str(tmp_path),
        clock=_clock(["2026-09-27T00:00:01Z", "2026-09-27T00:00:02Z"]),
    )
    index.create("old", "/work/old")
    index.create("new", "/work/new/")
    _thread(
        tmp_path,
        "old",
        [{"role": "user", "content": "<technical>secret</technical>\nhidden"}],
    )
    _thread(
        tmp_path,
        "old",
        [{"role": "user", "content": "видимая\nвторая", "id": "u2"}],
    )
    long = "я" * 90
    _thread(tmp_path, "new", [{"role": "user", "content": long}])
    listed = index.list_sessions(None)
    assert [item["sessionId"] for item in listed] == ["new", "old"]
    assert listed[0]["cwd"] == "/work/new"
    assert listed[0]["title"] == "я" * 80
    assert listed[1]["title"] == "видимая"
    assert "nextCursor" not in json.dumps(listed)


def test_list_skips_sessions_without_acp_thread_and_filters_cwd(tmp_path):
    index = SessionIndex(
        str(tmp_path),
        clock=_clock(["2026-09-27T00:00:01Z", "2026-09-27T00:00:01Z"]),
    )
    index.create("bare", "/work")
    index.create("kept", "/work")
    _thread(tmp_path, "kept", [{"role": "assistant", "content": "нет user"}])
    (tmp_path / "data_sdk_bare.jsonl").write_text("{}\n", encoding="utf-8")
    assert index.list_sessions(None) == [
        {
            "sessionId": "kept",
            "cwd": "/work",
            "updatedAt": "2026-09-27T00:00:01Z",
        }
    ]
    assert index.list_sessions("/other") == []
    assert index.list_sessions("/work")[0]["sessionId"] == "kept"


def test_touch_and_remove(tmp_path):
    index = SessionIndex(
        str(tmp_path),
        clock=_clock(["2026-09-27T00:00:01Z", "2026-09-27T00:00:09Z"]),
    )
    index.create("s1", "/work")
    _thread(tmp_path, "s1", [{"role": "user", "content": "hi"}])
    index.touch("s1")
    assert index.load("s1").updated_at == "2026-09-27T00:00:09Z"
    index.remove("s1")
    assert index.load("s1") is None
    assert not (tmp_path / "data_acp_s1.jsonl").exists()
    assert index.load("missing") is None
