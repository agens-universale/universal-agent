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


def _call(name: str, call_id: str, arguments: dict) -> dict:
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
    }


def _out(call_id: str, *, ok: bool = True) -> dict:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps({"ok": ok}),
    }


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


def test_artifacts_keep_successful_writes_inside_cwd(tmp_path):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    nested = cwd / "sub"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    names = {
        "written.txt": b"w",
        "appended.txt": b"a",
        "edited.txt": b"e",
        "replaced.txt": b"r",
        "saved.txt": b"s",
        "dry.txt": b"d",
        "failed.txt": b"f",
        "pending.txt": b"p",
        "via.txt": b"v",
        "dup.txt": b"1",
        "later.txt": b"2",
    }
    for name, data in names.items():
        (cwd / name).write_bytes(data)
    (nested / "nested.txt").write_bytes(b"n")
    (cwd / "dir").mkdir()
    link = cwd / "link.txt"
    link.symlink_to(outside)
    inner = cwd / "inner.txt"
    inner.symlink_to(cwd / "via.txt")
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    abs_written = str(cwd / "written.txt")
    _jsonl(
        index,
        "files",
        [
            _user(f"запиши {abs_written}"),
            _call("fs_write_file", "w", {"path": abs_written}),
            _out("w"),
            _call(
                "fs_append_file",
                "a",
                {"path": str(nested / "nested.txt")},
            ),
            _out("a"),
            _call(
                "fs_apply_file_edits",
                "e",
                {"path": str(cwd / "edited.txt")},
            ),
            _out("e"),
            _call(
                "fs_search_and_replace",
                "r",
                {"path": str(cwd / "replaced.txt"), "dry_run": False},
            ),
            _out("r"),
            _call(
                "fs_save_base64_file",
                "s",
                {"path": "saved.txt"},
            ),
            _out("s"),
            _call("fs_write_file", "dup", {"path": abs_written}),
            _out("dup"),
            _call("fs_append_file", "later", {"path": str(cwd / "later.txt")}),
            _out("later"),
            _call(
                "fs_search_and_replace",
                "dry",
                {"path": str(cwd / "dry.txt"), "dry_run": True},
            ),
            _out("dry"),
            _call("fs_write_file", "bad", {"path": str(cwd / "failed.txt")}),
            _out("bad", ok=False),
            _call(
                "fs_write_file",
                "pend",
                {"path": str(cwd / "pending.txt")},
            ),
            _call(
                "exec_command",
                "sh",
                {"command": "touch " + str(cwd / "via.txt")},
            ),
            _out("sh"),
            _call(
                "exec_python",
                "py",
                {"code": "open('via.txt','w')"},
            ),
            _out("py"),
            _call(
                "fs_write_file",
                "up",
                {"path": str(cwd / "sub" / ".." / "via.txt")},
            ),
            _out("up"),
            _call("fs_write_file", "out", {"path": str(link)}),
            _out("out"),
            _call("fs_write_file", "dir", {"path": str(cwd / "dir")}),
            _out("dir"),
            _call("fs_write_file", "miss", {"path": str(cwd / "missing.txt")}),
            _out("miss"),
            _call("fs_write_file", "in", {"path": str(inner)}),
            _out("in"),
        ],
    )
    sidecar = tmp_path / "threads" / "data_subagents_responses_files.jsonl"
    sidecar.write_text(
        json.dumps(
            {
                "type": "message",
                "system": "responses",
                "payload": _call(
                    "fs_write_file",
                    "side",
                    {"path": str(cwd / "appended.txt")},
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = TestClient(app)
    response = client.get("/v1/dialogs/files/artifacts")
    assert response.status_code == 200
    assert response.json() == [
        {"path": "written.txt", "name": "written.txt"},
        {"path": "sub/nested.txt", "name": "nested.txt"},
        {"path": "edited.txt", "name": "edited.txt"},
        {"path": "replaced.txt", "name": "replaced.txt"},
        {"path": "saved.txt", "name": "saved.txt"},
        {"path": "later.txt", "name": "later.txt"},
        {"path": "inner.txt", "name": "inner.txt"},
    ]
    missing = client.get("/v1/dialogs/no-such/artifacts")
    assert missing.status_code == 404


def test_download_returns_bytes_and_rejects_unlisted(tmp_path, capsys):
    app, index, _threads = _joint(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    payload = b"\x00\xff<html>no</html>"
    (cwd / "note.txt").write_bytes(payload)
    (cwd / "other.txt").write_bytes(b"other")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"SECRET-FILE")
    _save(index, n=1, thread_id="files", cwd=str(cwd), created=1)
    _jsonl(
        index,
        "files",
        [
            _call("fs_write_file", "w", {"path": str(cwd / "note.txt")}),
            _out("w"),
        ],
    )
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
    assert other.status_code == 404
    assert other.content != b"other"
    climb = client.get(
        "/v1/dialogs/files/artifacts/download",
        params={"path": "../secret.txt"},
    )
    assert climb.status_code == 404
    assert b"SECRET-FILE" not in climb.content
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
    _jsonl(
        index,
        "files",
        [
            _user("есть"),
            _call("fs_write_file", "w", {"path": str(cwd / "note.txt")}),
            _out("w"),
        ],
    )
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
