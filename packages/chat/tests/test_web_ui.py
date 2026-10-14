"""Страница чата, список диалогов Responses и файлы хода."""

import json
import os
import uuid
from datetime import timedelta
from urllib.parse import quote

from fastapi.testclient import TestClient
from gc_chat.app import create_app
from gc_chat.completions.facade import CompletionsFacade
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.index_store import (
    ResponseIndex,
    ResponseRecord,
    thread_key,
)
from gc_core.threads.facade import run_sync
from gc_core.threads.models import (
    LeaseRequest,
    SessionState,
    ThreadChange,
    ThreadKey,
)
from gc_testkit.settings import make_settings

# Host-guard пускает только loopback (gc_chat.http_guard).
LOOPBACK = "http://127.0.0.1"

_TOKEN = "client-secret"


def _rid(n: int) -> str:
    return f"resp_{n:08x}-0000-4000-8000-000000000000"


def _joint(tmp_path, *, token: str = "", launch_root: str | None = None):
    threads = tmp_path / "threads"
    threads.mkdir(exist_ok=True)
    settings = make_settings(
        {
            "agent.model": "agent-model",
            "chat_api.auth_token": token,
            "chat_api.launch_root": launch_root,
            "paths.threads_data_path": str(threads),
        }
    )

    async def factory(settings, **kwargs):
        del settings, kwargs
        raise AssertionError("agent")

    index = ResponseIndex.for_threads_dir(str(threads))
    responses = ResponsesFacade(
        settings=settings, agent_factory=factory, index=index
    )
    completions = CompletionsFacade(settings=settings, agent_factory=factory)
    return create_app(responses, completions), index, threads


def _save(
    index: ResponseIndex,
    *,
    n: int,
    thread_id: str,
    cwd: str,
    created: int,
    status: str = "completed",
    previous: str | None = None,
    output: list | None = None,
) -> str:
    response_id = _rid(n)
    body = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "previous_response_id": previous,
        "output": list(output or []),
    }
    run_sync(
        index.save(
            ResponseRecord(
                response_id,
                thread_id,
                cwd,
                previous,
                created,
                status,
                body,
            )
        )
    )
    return response_id


def _jsonl(
    index: ResponseIndex,
    thread_id: str,
    payloads: list[dict],
    *,
    state: SessionState | None = None,
    key: ThreadKey | None = None,
) -> None:
    """История треда — одним шагом исполнителя, дословно (без отсева
    повторов, который делает дописывание сессии)."""
    run_sync(_step(index, key or thread_key(thread_id), payloads, state))


async def _step(index, key, payloads, state) -> None:
    store = index.threads.store
    record = await store.claim(
        key, LeaseRequest(owner="test", ttl=timedelta(minutes=1))
    )
    await store.commit(
        key,
        ThreadChange(
            op_id=uuid.uuid4().hex,
            expected_revision=record.revision,
            lease=record.lease,
            append_messages=payloads,
            set_state=state,
        ),
    )
    await store.release(key, record.lease)


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def test_root_is_html_without_token_and_hides_endpoint_fields(tmp_path):
    app, _index, _threads = _joint(tmp_path, token=_TOKEN)
    client = TestClient(app, base_url=LOOPBACK)
    response = client.get("/")
    assert response.status_code == 200
    ctype = response.headers["content-type"]
    assert ctype.startswith("text/html")
    assert "utf-8" in ctype.lower()
    html = response.text
    assert "/v1/responses" in html
    assert "/v1/chat/completions" in html
    assert "/v1/dialogs" in html
    assert "/v1/workspace/artifacts" in html
    assert 'id="f-token"' in html
    assert 'id="f-api"' in html
    assert 'id="f-host"' not in html
    assert 'id="f-port"' not in html
    assert 'id="f-scheme"' not in html
    assert 'id="dialogList"' in html
    assert 'id="artifactList"' in html
    assert html.index('id="sidebar"') < html.index('id="dialogList"')
    assert html.index('id="log"') < html.index('id="filesbar"')
    assert html.index('id="filesbar"') < html.index('id="artifactList"')
    assert 'id="dialogsToggle"' in html
    assert 'id="chatToggle"' in html
    assert ">Чат</button>" in html
    assert 'id="filesToggle"' in html
    assert 'id="paneTabs"' in html
    assert 'id="dialogsTab"' in html
    assert 'id="filesTab"' in html
    assert 'id="chatTab"' in html
    assert "max-width: 1199px" in html
    assert "aside.collapsed, #chatpane.collapsed { display: none; }" in html
    assert "gc-ua-chat-panes" in html
    assert "Папка пуста." in html
    assert "gc-ua-chat-thread" in html
    assert "showFilesForCurrent" in html
    assert "Просмотреть" in html
    assert "item.text" in html
    assert "white-space:pre-wrap" in html
    assert 'data-line="' in html
    assert "FILE_PATH" in html
    assert "'L'+start+'-L'+end" in html
    assert "Скопировано" in html
    assert "Ctrl+C или «Копировать»" in html
    assert "mouseup" not in html.split("function previewPage", 1)[-1]
    assert "Скачать" in html
    assert "Скопировать путь" in html
    assert "Открыть" in html
    assert "Новая папка" in html
    assert "Удалить" in html
    assert 'class="prose trace"' in html
    assert 'class="copy-md"' in html
    assert ">Скопировать</span>" in html
    assert 'class="table-wrap"' in html
    assert "<summary>Рассуждение</summary><pre>" not in html


def test_root_does_not_serve_the_workdir(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-BYTES-XYZ", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    app, _index, _threads = _joint(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    root = client.get("/")
    assert "SECRET-BYTES-XYZ" not in root.text
    leaked = client.get("/secret.txt")
    assert leaked.status_code == 404
    assert b"SECRET-BYTES-XYZ" not in leaked.content


def test_dialogs_group_by_thread_head_and_updated_order(tmp_path):
    app, index, _threads = _joint(tmp_path)
    cwd_old = tmp_path / "old"
    cwd_new = tmp_path / "new"
    cwd_old.mkdir()
    cwd_new.mkdir()
    first = _save(
        index,
        n=1,
        thread_id="thread-old",
        cwd=str(cwd_old),
        created=100,
        status="completed",
    )
    head = _save(
        index,
        n=2,
        thread_id="thread-old",
        cwd=str(cwd_old),
        created=200,
        status="incomplete",
        previous=first,
    )
    _save(
        index,
        n=3,
        thread_id="thread-new",
        cwd=str(cwd_new),
        created=300,
        status="completed",
    )
    _jsonl(
        index,
        "thread-old",
        [
            _user('<technical type="technical">служебное</technical>'),
            _user("х" * 120),
            _user("второй"),
        ],
    )
    _jsonl(
        index,
        "thread-new",
        [_user("  новый\nдиалог  ")],
    )
    client = TestClient(app, base_url=LOOPBACK)
    body = client.get("/v1/dialogs").json()
    assert [item["thread_id"] for item in body] == [
        "thread-new",
        "thread-old",
    ]
    newest = body[0]
    assert newest["latest_response_id"] == _rid(3)
    assert newest["cwd"] == os.path.normpath(str(cwd_new))
    assert newest["created_at"] == 300
    assert newest["updated_at"] == 300
    assert newest["status"] == "completed"
    assert newest["title"] == "новый диалог"
    older = body[1]
    assert older["latest_response_id"] == head
    assert older["created_at"] == 100
    assert older["updated_at"] == 200
    assert older["status"] == "incomplete"
    assert older["title"] == "х" * 120


def test_dialogs_skip_other_systems_and_broken_index(tmp_path):
    app, index, threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    _save(index, n=1, thread_id="kept", cwd=str(cwd), created=10)
    _jsonl(index, "kept", [_user("виден")])
    _jsonl(
        index,
        "ephemeral",
        [_user("эфемерный")],
        key=ThreadKey(system="completions", thread="ephemeral"),
    )
    # Ответ develop, испорченный на диске, — пропускается.
    broken = threads.parent / "responses-index" / (_rid(9) + ".json")
    broken.parent.mkdir()
    broken.write_text("{", encoding="utf-8")
    _save(index, n=4, thread_id="no-jsonl", cwd=str(cwd), created=5)
    _save(index, n=5, thread_id="bad-jsonl", cwd=str(cwd), created=4)
    # История, испорченная чужим писателем, — без строк, которые не читаются.
    bad_jsonl = threads / "data_responses_bad-jsonl.jsonl"
    bad_jsonl.write_text("{not json}\n", encoding="utf-8")
    client = TestClient(app, base_url=LOOPBACK)
    body = client.get("/v1/dialogs").json()
    ids = [item["thread_id"] for item in body]
    assert "kept" in ids
    assert "ephemeral" not in ids
    assert "no-jsonl" in ids
    assert "bad-jsonl" in ids
    by_id = {item["thread_id"]: item for item in body}
    assert by_id["kept"]["title"] == "виден"
    assert by_id["no-jsonl"]["title"] == ""
    assert by_id["bad-jsonl"]["title"] == ""
    assert "эфемерный" not in json.dumps(body)


def _call(name: str, arguments: dict) -> dict:
    return {
        "type": "function_call",
        "name": name,
        "call_id": name,
        "arguments": json.dumps(arguments),
    }


def test_dialog_title_follows_tui_prompt_and_task(tmp_path):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    long_prompt = "переписать разбор заголовка диалога целиком"
    later = "ещё более длинная формулировка следующей задачи"
    _save(index, n=1, thread_id="short-then-long", cwd=str(cwd), created=10)
    _jsonl(
        index,
        "short-then-long",
        [_user("ок"), _user(long_prompt), _user("спасибо")],
    )
    _save(index, n=2, thread_id="task-wins", cwd=str(cwd), created=20)
    _jsonl(
        index,
        "task-wins",
        [
            _user(long_prompt),
            _call("session_think", {"thought": "думаю"}),
            _call("turn_finish", {"task": "  сжать\nлог  "}),
            _user("пока"),
            _call("turn_finish", {"task": "не должно сменить"}),
            _user(later),
            _call("fs_read_file", {"path": "a"}),
            _call("turn_finish", {"task": "прочитать файл"}),
        ],
    )
    _save(index, n=3, thread_id="only-short", cwd=str(cwd), created=5)
    _jsonl(index, "only-short", [_user("ты тут?")])
    client = TestClient(app, base_url=LOOPBACK)
    by_id = {
        item["thread_id"]: item for item in client.get("/v1/dialogs").json()
    }
    assert by_id["short-then-long"]["title"] == long_prompt
    assert by_id["only-short"]["title"] == "ты тут?"
    assert by_id["task-wins"]["title"] == "прочитать файл"


def _ask(text: str, turn: int = 1) -> dict:
    return {
        "turn": turn,
        "ts": "2026-10-01T12:00:00Z",
        "action": "user_ask",
        "tool_name": None,
        "content": text,
    }


def _intents(logged: list[dict], current: list[dict] | None = None):
    """Журнал намерений треда — как его ведёт ядро."""
    return SessionState(intent_log=logged, current_intent=current or [])


def test_dialog_title_reads_intent_log_after_summary(tmp_path):
    app, index, threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    long_prompt = "переписать разбор заголовка диалога целиком"
    later = "ещё более длинная формулировка следующей задачи после сводки"
    summary = '<technical type="dialog_summary">сводка без имени</technical>'
    _save(index, n=1, thread_id="summed", cwd=str(cwd), created=10)
    _jsonl(
        index,
        "summed",
        [_user(summary), _user("ок")],
        state=_intents(
            [_ask("ты тут?"), _ask(long_prompt, 2)], [_ask("спасибо", 3)]
        ),
    )
    _save(index, n=2, thread_id="open-ask", cwd=str(cwd), created=20)
    _jsonl(
        index,
        "open-ask",
        [_user(summary)],
        state=_intents([_ask("ты тут?")], [_ask(later, 2)]),
    )
    _save(index, n=3, thread_id="task-still-wins", cwd=str(cwd), created=30)
    _jsonl(
        index,
        "task-still-wins",
        [
            _user(long_prompt),
            _call("fs_read_file", {"path": "a"}),
            _call("turn_finish", {"task": "сжать лог"}),
        ],
        state=_intents([_ask(later)]),
    )
    client = TestClient(app, base_url=LOOPBACK)
    by_id = {
        item["thread_id"]: item for item in client.get("/v1/dialogs").json()
    }
    assert by_id["summed"]["title"] == long_prompt
    assert by_id["open-ask"]["title"] == later
    assert by_id["task-still-wins"]["title"] == "сжать лог"


def test_dialog_with_broken_meta_does_not_break_the_list(tmp_path):
    """Сводка диалога v2 лежит в meta треда: испорченная meta уносит диалог
    из списка (в develop он оставался без имени), остальные — на месте."""
    app, index, threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    _save(index, n=1, thread_id="kept", cwd=str(cwd), created=10)
    _save(index, n=2, thread_id="broken-meta", cwd=str(cwd), created=5)
    meta = threads / "meta_responses_broken-meta.json"
    meta.write_text("{not json", encoding="utf-8")
    client = TestClient(app, base_url=LOOPBACK)
    response = client.get("/v1/dialogs")
    assert response.status_code == 200
    assert [item["thread_id"] for item in response.json()] == ["kept"]


def test_dialog_feed_is_chain_order_with_text_and_output(tmp_path):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    output_a = [
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "раз"}],
        }
    ]
    output_b = [
        {
            "type": "gc_tool_call",
            "name": "fs_write_file",
            "status": "completed",
        }
    ]
    first = _save(
        index,
        n=1,
        thread_id="chain",
        cwd=str(cwd),
        created=10,
        output=output_a,
    )
    second = _save(
        index,
        n=2,
        thread_id="chain",
        cwd=str(cwd),
        created=20,
        status="completed",
        previous=first,
        output=output_b,
    )
    _jsonl(index, "chain", [_user("первый"), _user("второй")])
    client = TestClient(app, base_url=LOOPBACK)
    response = client.get("/v1/dialogs/chain")
    assert response.status_code == 200
    turns = response.json()
    assert [item["response_id"] for item in turns] == [first, second]
    assert turns[0]["previous_response_id"] is None
    assert turns[1]["previous_response_id"] == first
    assert turns[0]["text"] == "первый"
    assert turns[1]["text"] == "второй"
    assert turns[0]["output"] == output_a
    assert turns[1]["output"] == output_b
    assert turns[0]["created_at"] == 10
    assert turns[1]["status"] == "completed"
    missing = client.get("/v1/dialogs/missing-thread")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    invalid = client.get("/v1/dialogs/bad id")
    assert invalid.status_code == 404


def test_folder_lists_one_level_without_tool_calls(tmp_path):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    nested = cwd / "sub"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    (cwd / "Later.txt").write_bytes(b"2")
    (cwd / ".env").write_bytes(b"k")
    (cwd / ".gc").mkdir()
    (cwd / ".gc" / "config.yml").write_bytes(b"c")
    (cwd / "via.txt").write_bytes(b"v")
    (nested / "nested.txt").write_bytes(b"n")
    (nested / ".hidden").write_bytes(b"h")
    (cwd / "Docs").mkdir()
    (cwd / "link.txt").symlink_to(outside)
    (cwd / "inner.txt").symlink_to(cwd / "via.txt")
    (cwd / "inside").symlink_to(nested)
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    _jsonl(
        index,
        "files",
        [
            _user(
                '<technical type="dialog_summary">сводка без тулов</technical>'
            )
        ],
    )
    client = TestClient(app, base_url=LOOPBACK)
    response = client.get("/v1/dialogs/files/artifacts")
    assert response.status_code == 200
    assert response.json() == {
        "path": "",
        "entries": [
            {"name": "Docs", "path": "Docs", "kind": "dir"},
            {"name": "inside", "path": "inside", "kind": "dir"},
            {"name": "sub", "path": "sub", "kind": "dir"},
            {
                "name": "inner.txt",
                "path": "inner.txt",
                "kind": "file",
                "text": True,
            },
            {
                "name": "Later.txt",
                "path": "Later.txt",
                "kind": "file",
                "text": True,
            },
            {
                "name": "via.txt",
                "path": "via.txt",
                "kind": "file",
                "text": True,
            },
        ],
    }
    nested_list = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "sub"}
    )
    assert nested_list.status_code == 200
    assert nested_list.json() == {
        "path": "sub",
        "entries": [
            {
                "name": "nested.txt",
                "path": "sub/nested.txt",
                "kind": "file",
                "text": True,
            },
        ],
    }
    via_link = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "inside"}
    )
    assert via_link.json()["entries"] == [
        {
            "name": "nested.txt",
            "path": "inside/nested.txt",
            "kind": "file",
            "text": True,
        },
    ]
    missing = client.get("/v1/dialogs/no-such/artifacts")
    assert missing.status_code == 404
    climb = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "../outside.txt"}
    )
    assert climb.status_code == 404
    absolute = client.get(
        "/v1/dialogs/files/artifacts", params={"path": str(outside)}
    )
    assert absolute.status_code == 404
    as_file = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "via.txt"}
    )
    assert as_file.status_code == 404
    escaped = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "link.txt"}
    )
    assert escaped.status_code == 404
    hidden = client.get("/v1/dialogs/files/artifacts", params={"path": ".gc"})
    assert hidden.status_code == 404
    nested_hidden = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "sub/.hidden"}
    )
    assert nested_hidden.status_code == 404


def test_folder_marks_text_and_binary(tmp_path):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "note.md").write_bytes("заголовок\n".encode())
    (cwd / "empty.txt").write_bytes(b"")
    (cwd / "blob.bin").write_bytes(b"\x00\x01")
    (cwd / "latin.bin").write_bytes(b"\xff\xfe")
    split = b"a" * 8191 + "я".encode()
    (cwd / "split.txt").write_bytes(split)
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    client = TestClient(app, base_url=LOOPBACK)
    by_name = {
        item["name"]: item
        for item in client.get("/v1/dialogs/files/artifacts").json()["entries"]
    }
    assert by_name["note.md"]["text"] is True
    assert by_name["empty.txt"]["text"] is True
    assert by_name["split.txt"]["text"] is True
    assert by_name["blob.bin"]["text"] is False
    assert by_name["latin.bin"]["text"] is False


def test_download_returns_any_file_inside_cwd(tmp_path, capsys):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    nested = cwd / "sub"
    nested.mkdir(parents=True)
    payload = b"\x00\xff<html>no</html>"
    (cwd / "note.txt").write_bytes(payload)
    (cwd / ".env").write_bytes(b"secret-env")
    (cwd / "other.txt").write_bytes(b"other")
    (nested / "nested.txt").write_bytes(b"nested")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"SECRET-FILE")
    (cwd / "link.txt").symlink_to(outside)
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    client = TestClient(app, base_url=LOOPBACK)
    got = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "note.txt"},
    )
    assert got.status_code == 200
    assert got.content == payload
    assert got.headers["content-type"].startswith("application/octet-stream")
    disposition = got.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "note.txt" in disposition
    report = cwd / "репорт.txt"
    report.write_bytes("отчёт".encode())
    (cwd / "报告.txt").write_bytes(b"zh")
    cyrillic = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "репорт.txt"},
    )
    assert cyrillic.status_code == 200
    assert cyrillic.content == "отчёт".encode()
    header = cyrillic.headers["content-disposition"]
    header.encode("latin-1")
    assert header == "attachment; filename*=UTF-8''" + quote(
        "репорт.txt", safe=""
    )
    han = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "报告.txt"},
    )
    assert han.status_code == 200
    assert han.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''" + quote("报告.txt", safe="")
    )
    err = capsys.readouterr()
    assert payload not in err.err.encode("utf-8", errors="replace")
    assert b"<html>" not in err.err.encode("utf-8", errors="replace")
    other = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "other.txt"},
    )
    assert other.status_code == 200
    assert other.content == b"other"
    nested_file = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "sub/nested.txt"},
    )
    assert nested_file.status_code == 200
    assert nested_file.content == b"nested"
    dot = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": ".env"},
    )
    assert dot.status_code == 404
    assert b"secret-env" not in dot.content
    climb = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "../secret.txt"},
    )
    assert climb.status_code == 404
    assert b"SECRET-FILE" not in climb.content
    linked = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "link.txt"},
    )
    assert linked.status_code == 404
    assert b"SECRET-FILE" not in linked.content
    folder = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "sub"},
    )
    assert folder.status_code == 404
    empty = client.get("/v1/dialogs/files/artifacts/download")
    assert empty.status_code == 404
    (cwd / "note.txt").unlink()
    gone = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "note.txt"},
    )
    assert gone.status_code == 404


def test_mkdir_and_delete_stay_inside_cwd(tmp_path):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    nested = cwd / "sub"
    nested.mkdir(parents=True)
    (cwd / "note.txt").write_bytes(b"keep")
    (nested / "inner.txt").write_bytes(b"in")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"SECRET-FILE")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_bytes(b"SECRET-DIR")
    (cwd / "link.txt").symlink_to(cwd / "note.txt")
    (cwd / "out-link").symlink_to(outside)
    (nested / "escape").symlink_to(outside_dir)
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    client = TestClient(app, base_url=LOOPBACK)

    created = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "", "name": "qwen"},
    )
    assert created.status_code == 200
    assert created.json() == {"path": "qwen", "kind": "dir"}
    assert (cwd / "qwen").is_dir()
    nested_dir = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "sub", "name": " leaf "},
    )
    assert nested_dir.status_code == 200
    assert nested_dir.json()["path"] == "sub/leaf"
    assert (nested / "leaf").is_dir()
    again = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "", "name": "qwen"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["message"] == "already exists"
    bad_name = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "", "name": "../outside-dir"},
    )
    assert bad_name.status_code == 400
    hidden = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "", "name": ".secret"},
    )
    assert hidden.status_code == 400
    missing_parent = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "nope", "name": "qwen"},
    )
    assert missing_parent.status_code == 404
    assert outside_dir.is_dir()

    removed_link = client.delete(
        "/v1/dialogs/files/artifacts",
        params={"path": "link.txt"},
    )
    assert removed_link.status_code == 200
    assert not (cwd / "link.txt").exists()
    assert (cwd / "note.txt").read_bytes() == b"keep"
    removed_file = client.delete(
        "/v1/dialogs/files/artifacts",
        params={"path": "note.txt"},
    )
    assert removed_file.status_code == 200
    assert not (cwd / "note.txt").exists()
    removed_tree = client.delete(
        "/v1/dialogs/files/artifacts",
        params={"path": "sub"},
    )
    assert removed_tree.status_code == 200
    assert not nested.exists()
    assert outside.read_bytes() == b"SECRET-FILE"
    assert (outside_dir / "secret.txt").read_bytes() == b"SECRET-DIR"
    root = client.delete("/v1/dialogs/files/artifacts", params={"path": ""})
    assert root.status_code == 404
    dot = client.delete("/v1/dialogs/files/artifacts", params={"path": "."})
    assert dot.status_code == 404
    climb = client.delete(
        "/v1/dialogs/files/artifacts",
        params={"path": "../outside.txt"},
    )
    assert climb.status_code == 404
    escaped = client.delete(
        "/v1/dialogs/files/artifacts",
        params={"path": "out-link"},
    )
    assert escaped.status_code == 404
    assert outside.read_bytes() == b"SECRET-FILE"
    assert cwd.is_dir()
    assert (cwd / "qwen").is_dir()

    app.state.responses_facade._busy.add("files")
    while_busy = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "", "name": "later"},
    )
    assert while_busy.status_code == 409
    assert while_busy.json()["error"]["message"] == "turn already running"
    assert not (cwd / "later").exists()
    delete_busy = client.delete(
        "/v1/dialogs/files/artifacts",
        params={"path": "qwen"},
    )
    assert delete_busy.status_code == 409
    assert (cwd / "qwen").is_dir()


def test_workspace_lists_new_dialog_root(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    nested = root / "src"
    nested.mkdir(parents=True)
    (root / "readme.txt").write_bytes(b"a")
    (root / ".env").write_bytes(b"k")
    (nested / "main.py").write_bytes(b"p")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"no")
    (root / "link.txt").symlink_to(outside)
    app, _index, _threads = _joint(tmp_path, launch_root=str(root))
    client = TestClient(app, base_url=LOOPBACK)
    listed = client.get("/v1/workspace/artifacts")
    assert listed.status_code == 200
    assert listed.json() == {
        "cwd": os.path.normpath(str(root)),
        "path": "",
        "entries": [
            {"name": "src", "path": "src", "kind": "dir"},
            {
                "name": "readme.txt",
                "path": "readme.txt",
                "kind": "file",
                "text": True,
            },
        ],
    }
    inside = client.get("/v1/workspace/artifacts", params={"path": "src"})
    assert inside.json() == {
        "cwd": os.path.normpath(str(root)),
        "path": "src",
        "entries": [
            {
                "name": "main.py",
                "path": "src/main.py",
                "kind": "file",
                "text": True,
            },
        ],
    }
    assert (
        client.get(
            "/v1/workspace/artifacts", params={"path": "../outside.txt"}
        ).status_code
        == 404
    )
    hidden = client.get("/v1/workspace/artifacts", params={"path": ".env"})
    assert hidden.status_code == 404
    assert (
        client.get(
            "/v1/workspace/artifacts", params={"path": "link.txt"}
        ).status_code
        == 404
    )
    missing = _joint(tmp_path, launch_root=str(tmp_path / "absent"))
    absent = TestClient(missing[0], base_url=LOOPBACK).get(
        "/v1/workspace/artifacts"
    )
    assert absent.status_code == 404
    monkeypatch.chdir(tmp_path)
    (tmp_path / "here.txt").write_bytes(b"h")
    process = TestClient(_joint(tmp_path)[0], base_url=LOOPBACK).get(
        "/v1/workspace/artifacts"
    )
    assert process.json()["cwd"] == os.path.normpath(str(tmp_path))
    assert {
        "name": "here.txt",
        "path": "here.txt",
        "kind": "file",
        "text": True,
    } in process.json()["entries"]
    relative = TestClient(
        _joint(tmp_path, launch_root="relative/path")[0], base_url=LOOPBACK
    ).get("/v1/workspace/artifacts")
    assert relative.json()["cwd"] == os.path.normpath(str(tmp_path))
    created = client.post(
        "/v1/workspace/artifacts", json={"path": "", "name": "notes"}
    )
    assert created.status_code == 200
    assert created.json() == {"path": "notes", "kind": "dir"}
    assert (root / "notes").is_dir()
    downloaded = client.get(
        "/v1/workspace/artifacts/download", params={"path": "readme.txt"}
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"a"
    removed = client.delete(
        "/v1/workspace/artifacts", params={"path": "notes"}
    )
    assert removed.status_code == 200
    assert not (root / "notes").exists()


def test_v1_dialogs_require_bearer_and_root_stays_open(tmp_path):
    app, index, _threads = _joint(tmp_path, token=_TOKEN)
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "note.txt").write_bytes(b"x")
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    _jsonl(index, "files", [_user("есть")])
    client = TestClient(app, base_url=LOOPBACK)
    paths = [
        "/v1/dialogs",
        "/v1/dialogs/files",
        "/v1/dialogs/files/artifacts",
        "/v1/dialogs/files/artifacts/download?path=note.txt",
        "/v1/workspace/artifacts",
        "/v1/workspace/artifacts/download?path=note.txt",
    ]
    for path in paths:
        denied = client.get(path)
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "invalid_api_key"
    created = client.post(
        "/v1/dialogs/files/artifacts",
        json={"path": "", "name": "qwen"},
    )
    assert created.status_code == 401
    removed = client.delete(
        "/v1/dialogs/files/artifacts",
        params={"path": "note.txt"},
    )
    assert removed.status_code == 401
    workspace_created = client.post(
        "/v1/workspace/artifacts",
        json={"path": "", "name": "qwen"},
    )
    assert workspace_created.status_code == 401
    workspace_removed = client.delete(
        "/v1/workspace/artifacts",
        params={"path": "note.txt"},
    )
    assert workspace_removed.status_code == 401
    headers = {"Authorization": "Bearer " + _TOKEN}
    assert client.get("/v1/dialogs", headers=headers).status_code == 200
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
