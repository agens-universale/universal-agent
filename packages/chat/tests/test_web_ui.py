"""Страница чата, список диалогов Responses и файлы хода."""

import json
import os

from fastapi.testclient import TestClient
from gc_chat.app import create_app
from gc_chat.completions.facade import CompletionsFacade
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.index_store import ResponseIndex, ResponseRecord

_TOKEN = "client-secret"


def _rid(n: int) -> str:
    return f"resp_{n:08x}-0000-4000-8000-000000000000"


def _joint(tmp_path, *, token: str = ""):
    threads = tmp_path / "threads"
    threads.mkdir(exist_ok=True)
    config = {
        "model": "agent-model",
        "chat_api_auth_token": token,
        "threads_data_path": str(threads),
    }

    async def factory(config, **kwargs):
        del config, kwargs
        raise AssertionError("agent")

    index = ResponseIndex(str(threads))
    responses = ResponsesFacade(
        config=config, agent_factory=factory, index=index
    )
    completions = CompletionsFacade(config=config, agent_factory=factory)
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
    return response_id


def _jsonl(index: ResponseIndex, thread_id: str, payloads: list[dict]) -> None:
    path = index.thread_path(thread_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for seq, payload in enumerate(payloads, start=1):
            record = {
                "type": "message",
                "system": "responses",
                "seq": seq,
                "payload": payload,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def test_root_is_html_without_token_and_hides_endpoint_fields(tmp_path):
    app, _index, _threads = _joint(tmp_path, token=_TOKEN)
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    ctype = response.headers["content-type"]
    assert ctype.startswith("text/html")
    assert "utf-8" in ctype.lower()
    html = response.text
    assert "/v1/responses" in html
    assert "/v1/chat/completions" in html
    assert "/v1/dialogs" in html
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
    assert 'id="filesToggle"' in html
    assert "gc-ua-chat-panes" in html
    assert "Папка пуста." in html
    assert "gc-ua-chat-thread" in html
    assert "showFilesForCurrent" in html
    assert "Скачать" in html
    assert "Скопировать путь" in html
    assert "Открыть" in html
    assert 'class="prose trace"' in html
    assert 'class="table-wrap"' in html
    assert "<summary>Рассуждение</summary><pre>" not in html


def test_root_does_not_serve_the_workdir(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-BYTES-XYZ", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    app, _index, _threads = _joint(tmp_path)
    client = TestClient(app)
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
    client = TestClient(app)
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
    assert older["title"] == "х" * 80


def test_dialogs_skip_foreign_threads_and_broken_index(tmp_path):
    app, index, threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    _save(index, n=1, thread_id="kept", cwd=str(cwd), created=10)
    _jsonl(index, "kept", [_user("виден")])
    _save(index, n=2, thread_id="foreign", cwd=str(cwd), created=20)
    foreign = index.thread_path("foreign")
    with open(foreign, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "message",
                    "system": "completions",
                    "payload": _user("чужой"),
                }
            )
            + "\n"
        )
    comp = threads / "data_completions_ephemeral.jsonl"
    comp.write_text(
        json.dumps(
            {
                "type": "message",
                "system": "completions",
                "payload": _user("эфемерный"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    broken = threads.parent / "responses-index" / (_rid(9) + ".json")
    broken.write_text("{", encoding="utf-8")
    _save(index, n=4, thread_id="no-jsonl", cwd=str(cwd), created=5)
    bad_jsonl = index.thread_path("bad-jsonl")
    _save(index, n=5, thread_id="bad-jsonl", cwd=str(cwd), created=4)
    os.makedirs(os.path.dirname(bad_jsonl), exist_ok=True)
    with open(bad_jsonl, "w", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    client = TestClient(app)
    body = client.get("/v1/dialogs").json()
    ids = [item["thread_id"] for item in body]
    assert "kept" in ids
    assert "foreign" not in ids
    assert "ephemeral" not in ids
    assert "no-jsonl" in ids
    assert "bad-jsonl" in ids
    by_id = {item["thread_id"]: item for item in body}
    assert by_id["kept"]["title"] == "виден"
    assert by_id["no-jsonl"]["title"] == ""
    assert by_id["bad-jsonl"]["title"] == ""
    assert "чужой" not in json.dumps(body)
    assert "эфемерный" not in json.dumps(body)


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
    client = TestClient(app)
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
    client = TestClient(app)
    response = client.get("/v1/dialogs/files/artifacts")
    assert response.status_code == 200
    assert response.json() == {
        "path": "",
        "entries": [
            {"name": "Docs", "path": "Docs", "kind": "dir"},
            {"name": "inside", "path": "inside", "kind": "dir"},
            {"name": "sub", "path": "sub", "kind": "dir"},
            {"name": "inner.txt", "path": "inner.txt", "kind": "file"},
            {"name": "Later.txt", "path": "Later.txt", "kind": "file"},
            {"name": "via.txt", "path": "via.txt", "kind": "file"},
        ],
    }
    nested_list = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "sub"}
    )
    assert nested_list.status_code == 200
    assert nested_list.json() == {
        "path": "sub",
        "entries": [
            {"name": "nested.txt", "path": "sub/nested.txt", "kind": "file"},
        ],
    }
    via_link = client.get(
        "/v1/dialogs/files/artifacts", params={"path": "inside"}
    )
    assert via_link.json()["entries"] == [
        {"name": "nested.txt", "path": "inside/nested.txt", "kind": "file"},
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
    client = TestClient(app)
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


def test_v1_dialogs_require_bearer_and_root_stays_open(tmp_path):
    app, index, _threads = _joint(tmp_path, token=_TOKEN)
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "note.txt").write_bytes(b"x")
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    _jsonl(index, "files", [_user("есть")])
    client = TestClient(app)
    paths = [
        "/v1/dialogs",
        "/v1/dialogs/files",
        "/v1/dialogs/files/artifacts",
        "/v1/dialogs/files/artifacts/download?path=note.txt",
    ]
    for path in paths:
        denied = client.get(path)
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "invalid_api_key"
    headers = {"Authorization": "Bearer " + _TOKEN}
    assert client.get("/v1/dialogs", headers=headers).status_code == 200
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
