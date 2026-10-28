"""Сессии ACP в тредах system=acp: состояние ``frontend["acp"]``, список,
заголовок, файлы ``acp-sessions`` develop."""

import json

from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey

from gc_acp.session_index import SessionIndex, thread_key


def _clock(values):
    stream = iter(values)
    return lambda: next(stream)


def _index(tmp_path, *stamps):
    return SessionIndex(
        Threads(FileThreadStore(tmp_path)),
        legacy_dir=str(tmp_path),
        clock=_clock(stamps),
    )


async def _thread(index, session_id, messages):
    await index.threads.append_messages(thread_key(session_id), messages)


async def test_list_orders_by_updated_at_and_cuts_title(tmp_path):
    index = _index(tmp_path, "2026-09-27T00:00:01Z", "2026-09-27T00:00:02Z")
    await index.create("old", "/work/old")
    await index.create("new", "/work/new/")
    await _thread(
        index,
        "old",
        [{"role": "user", "content": "<technical>secret</technical>\nhidden"}],
    )
    await _thread(
        index,
        "old",
        [{"role": "user", "content": "видимая\nвторая", "id": "u2"}],
    )
    long = "я" * 90
    await _thread(index, "new", [{"role": "user", "content": long}])
    listed = await index.list_sessions(None)
    assert [item["sessionId"] for item in listed] == ["new", "old"]
    assert listed[0]["cwd"] == "/work/new"
    assert listed[0]["title"] == "я" * 80
    assert listed[1]["title"] == "видимая"
    assert "nextCursor" not in json.dumps(listed)


async def test_list_skips_sessions_without_turn_and_filters_cwd(tmp_path):
    index = _index(tmp_path, "2026-09-27T00:00:01Z", "2026-09-27T00:00:01Z")
    await index.create("bare", "/work")
    await index.create("kept", "/work")
    await _thread(
        index, "kept", [{"role": "assistant", "content": "нет user"}]
    )
    foreign = ThreadKey(system="sdk", thread="other")
    await index.threads.append_messages(foreign, [{"role": "user"}])
    assert await index.list_sessions(None) == [
        {
            "sessionId": "kept",
            "cwd": "/work",
            "updatedAt": "2026-09-27T00:00:01Z",
        }
    ]
    assert await index.list_sessions("/other") == []
    assert (await index.list_sessions("/work"))[0]["sessionId"] == "kept"


async def test_touch_caches_title_and_list_skips_history(
    tmp_path, monkeypatch
):
    index = _index(tmp_path, "2026-09-27T00:00:01Z", "2026-09-27T00:00:09Z")
    await index.create("s1", "/work")
    await _thread(index, "s1", [{"role": "user", "content": "привет"}])
    await index.touch("s1")
    assert (await index.load("s1")).updated_at == "2026-09-27T00:00:09Z"

    async def no_history(key):
        raise AssertionError("список читал историю")

    monkeypatch.setattr(index.threads, "history", no_history)
    assert (await index.list_sessions(None))[0]["title"] == "привет"


async def test_remove_deletes_the_thread(tmp_path):
    index = _index(tmp_path, "2026-09-27T00:00:01Z")
    await index.create("s1", "/work")
    await _thread(index, "s1", [{"role": "user", "content": "hi"}])
    await index.remove("s1")
    assert await index.load("s1") is None
    assert await index.threads.get(thread_key("s1")) is None
    assert await index.load("missing") is None
    assert await index.load("../bad") is None


async def test_dropped_record_keeps_history_and_touch_does_not_revive(
    tmp_path,
):
    index = _index(tmp_path, "2026-09-27T00:00:01Z", "2026-09-27T00:00:02Z")
    await index.create("s1", "/work")
    await _thread(index, "s1", [{"role": "user", "content": "hi"}])
    await index.drop_record("s1")
    await index.touch("s1")
    assert await index.load("s1") is None
    assert await index.list_sessions(None) == []
    assert len(await index.threads.history(thread_key("s1"))) == 1


async def test_develop_session_file_is_read_until_first_turn(tmp_path):
    index = _index(tmp_path, "2026-09-27T00:00:09Z")
    await _thread(index, "s1", [{"role": "user", "content": "старый"}])
    legacy = tmp_path / "acp-sessions" / "s1.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps({"cwd": "/work/", "updatedAt": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    loaded = await index.load("s1")
    assert (loaded.cwd, loaded.updated_at) == (
        "/work",
        "2026-01-01T00:00:00Z",
    )
    await index.touch("s1")
    legacy.unlink()
    record = await index.threads.get(thread_key("s1"))
    assert record.frontend["acp"] == {
        "cwd": "/work",
        "updated_at": "2026-09-27T00:00:09Z",
        "title": "старый",
    }
    assert (await index.load("s1")).cwd == "/work"


async def test_develop_session_without_turns_loads_but_is_not_listed(
    tmp_path,
):
    """Сессия develop без единого хода: файл ``acp-sessions`` есть, треда
    нет. develop её загружал (``session/load``), но в списке не показывал."""
    index = _index(tmp_path)
    legacy = tmp_path / "acp-sessions" / "fresh.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps({"cwd": "/work", "updatedAt": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    assert (await index.load("fresh")).cwd == "/work"
    assert await index.list_sessions(None) == []


def _legacy_file(tmp_path, session_id):
    path = tmp_path / "acp-sessions" / f"{session_id}.json"
    return (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    )


async def test_develop_session_files_are_written_alongside(tmp_path):
    """Откат на develop: его ``session/list``/``load`` читают только
    ``acp-sessions/*.json`` — файл живёт вместе с сессией ветки."""
    index = _index(tmp_path, "2026-09-27T00:00:01Z", "2026-09-27T00:00:02Z")
    await index.create("s1", "/work")
    assert _legacy_file(tmp_path, "s1") == {
        "cwd": "/work",
        "updatedAt": "2026-09-27T00:00:01Z",
    }
    await _thread(index, "s1", [{"role": "user", "content": "привет"}])
    await index.touch("s1")
    assert _legacy_file(tmp_path, "s1")["updatedAt"] == "2026-09-27T00:00:02Z"
    await index.remove("s1")
    assert _legacy_file(tmp_path, "s1") is None


async def test_one_unreadable_session_does_not_hide_the_list(tmp_path):
    """Сессия develop с битым UTF-8 в истории: список строится по meta, а
    заголовок — из истории; она пропускается, остальные — в списке."""
    index = _index(tmp_path, "2026-09-27T00:00:01Z")
    await index.create("good", "/work")
    await _thread(index, "good", [{"role": "user", "content": "x"}])
    (tmp_path / "meta_acp_bad.json").write_text(
        json.dumps({"thread_id": "bad", "system": "acp", "messages_count": 1}),
        encoding="utf-8",
    )
    (tmp_path / "data_acp_bad.jsonl").write_bytes(
        b'{"type": "message", "thread_id": "bad", "system": "acp", '
        b'"seq": 1, "ts": "2026-01-01T00:00:00Z", '
        b'"payload": {"role": "user", "content": "\xff\xfe"}}\n'
    )
    (tmp_path / "acp-sessions").mkdir(exist_ok=True)
    (tmp_path / "acp-sessions" / "bad.json").write_text(
        json.dumps({"cwd": "/work", "updatedAt": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    listed = await index.list_sessions(None)
    assert [item["sessionId"] for item in listed] == ["good"]
