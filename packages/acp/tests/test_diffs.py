"""Запись файла → diff в карточке тула. Клиент — память, вопроса нет."""

import asyncio
import json
from types import SimpleNamespace

from gc_acp.diffs import DiffBackend, DiffLog, _Side, attach_diff
from gc_acp.facade import AcpFacade
from gc_core.backends.local import LocalFsBackend
from gc_testkit.settings import make_settings

_LIMIT = 256 * 1024


class MemoryWriter:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class WritingAgent:
    def __init__(self):
        self.factory_kwargs = {}
        self.during = None
        self.last_answer = None
        self.executor = SimpleNamespace(
            _ctx=SimpleNamespace(),
            last_terminal_outcome=None,
        )

    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        if self.during is not None:
            await self.during()
        self.last_answer = "готово"
        yield ("answer", "готово")

    def stop(self):
        return None

    async def aclose(self):
        return None


class MemoryDisk:
    """Диск в памяти: чтение отсутствующего файла — FileNotFoundError."""

    def __init__(self):
        self.files = {}
        self.listed = []

    async def read_bytes(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_bytes(self, path, data):
        self.files[path] = bytes(data)

    async def listdir(self, path):
        self.listed.append(path)
        return []


class UnreadableDisk(MemoryDisk):
    async def read_bytes(self, path):
        raise OSError("denied")


def _call_id(value):
    """Текущий вызов для обёртки: ACP узнаёт его из хранилища, асинхронно."""

    async def current():
        return value

    return current


def _open(tmp_path, disk):
    agents = []

    async def factory(config, **kwargs):
        del config
        agent = WritingAgent()
        agent.factory_kwargs = kwargs
        agents.append(agent)
        return agent

    writer = MemoryWriter()
    facade = AcpFacade(
        settings=make_settings(
            {
                "paths.threads_data_path": str(tmp_path),
                "os_backend": "local",
            }
        ),
        writer=writer,
        agent_factory=factory,
        os_backend=disk,
    )
    return facade, writer, agents


def _line(method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body)


def _by_id(writer, request_id):
    found = [item for item in writer.sent if item.get("id") == request_id]
    assert found, writer.sent
    return found[-1]


def _updates(writer):
    return [
        item["params"]["update"]
        for item in writer.sent
        if item.get("method") == "session/update"
    ]


def _methods(writer):
    return [item.get("method") for item in writer.sent]


def _append(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"type": "message", "payload": payload}, ensure_ascii=False
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _call(path, name, call_id, arguments):
    _append(
        path,
        {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(arguments),
        },
    )


def _output(path, call_id, output):
    _append(
        path,
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(output),
        },
    )


def _completed(writer, call_id):
    for item in _updates(writer):
        if (
            item.get("sessionUpdate") == "tool_call_update"
            and item.get("toolCallId") == call_id
        ):
            return item
    return None


async def _wait_completed(writer, call_id):
    for _ in range(50):
        if _completed(writer, call_id) is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(writer.sent)


async def _session(facade, writer, cwd):
    await facade.handle_line(_line("initialize", {"protocolVersion": 1}))
    await facade.handle_line(
        _line("session/new", {"cwd": cwd, "mcpServers": []}, 2)
    )
    return _by_id(writer, 2)["result"]["sessionId"]


async def _prompt(facade, session_id):
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "правь"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task


def _thread(tmp_path, session_id):
    return tmp_path / f"data_acp_{session_id}.jsonl"


def _preview(payload):
    return {
        "type": "content",
        "content": {
            "type": "text",
            "text": json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ),
        },
    }


class _DiffBlocks:
    def content(self, call_id):
        if call_id != "w1":
            return None
        return [
            {
                "type": "diff",
                "path": "/abs/note.txt",
                "oldText": "было\n",
                "newText": "стало\n",
            }
        ]


def test_attach_diff_keeps_the_preview_ahead_of_the_diff():
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "w1",
        "status": "completed",
        "content": [_preview({"ok": True})],
    }
    shown = attach_diff(update, _DiffBlocks())
    assert shown["content"] == [
        _preview({"ok": True}),
        {
            "type": "diff",
            "path": "/abs/note.txt",
            "oldText": "было\n",
            "newText": "стало\n",
        },
    ]
    assert update["content"] == [_preview({"ok": True})]


def test_attach_diff_uses_the_oversize_note_after_the_preview():
    class _Oversize:
        def content(self, call_id):
            del call_id
            return [
                {
                    "type": "content",
                    "content": {
                        "type": "text",
                        "text": "/abs/big.txt: old 1 bytes, new 2 bytes",
                    },
                }
            ]

    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "big",
        "status": "completed",
        "content": [_preview({"ok": True})],
    }
    shown = attach_diff(update, _Oversize())
    assert shown["content"][0] == _preview({"ok": True})
    assert shown["content"][1]["content"]["text"].startswith("/abs/big.txt:")


def _diff_blocks(update):
    return [
        block
        for block in update.get("content") or []
        if block.get("type") == "diff"
    ]


async def test_write_reports_diff_and_lands_on_disk_without_asking(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    target = tmp_path / "note.txt"
    target.write_text("было\n", encoding="utf-8")
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "w1", {"path": str(target)})
        await backend.write_bytes(str(target), "стало\n".encode())
        assert await backend.read_bytes(str(target)) == "стало\n".encode()
        assert target.read_text(encoding="utf-8") == "стало\n"
        _output(path, "w1", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    update = _completed(writer, "w1")
    assert update["status"] == "completed"
    assert update["content"][0] == _preview({"ok": True})
    assert _diff_blocks(update) == [
        {
            "type": "diff",
            "path": str(target),
            "oldText": "было\n",
            "newText": "стало\n",
        }
    ]
    assert "kind" not in _diff_blocks(update)[0]
    started = next(
        item
        for item in _updates(writer)
        if item.get("sessionUpdate") == "tool_call"
        and item.get("toolCallId") == "w1"
    )
    assert "content" not in started
    assert "session/request_permission" not in _methods(writer)
    assert "fs/write_text_file" not in _methods(writer)
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_new_file_diff_has_null_old_text(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    target = tmp_path / "fresh.txt"
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "w1", {"path": str(target)})
        await backend.write_bytes(str(target), "новый\n".encode())
        _output(path, "w1", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    block = _diff_blocks(_completed(writer, "w1"))[0]
    assert block["oldText"] is None
    assert block["newText"] == "новый\n"
    assert block["path"] == str(target)
    assert target.read_text(encoding="utf-8") == "новый\n"
    await facade.shutdown()


async def test_five_edits_of_one_file_are_one_diff(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    target = tmp_path / "edit.txt"
    target.write_text("v0", encoding="utf-8")
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(
            path,
            "fs_apply_file_edits",
            "batch",
            {
                "path": str(target),
                "edits": [{"search": "v0", "replace": "v5"}],
            },
        )
        for version in range(1, 6):
            await backend.write_bytes(str(target), f"v{version}".encode())
        _output(path, "batch", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    update = _completed(writer, "batch")
    assert update["status"] == "completed"
    assert _diff_blocks(update) == [
        {
            "type": "diff",
            "path": str(target),
            "oldText": "v0",
            "newText": "v5",
        }
    ]
    assert target.read_text(encoding="utf-8") == "v5"
    await facade.shutdown()


async def test_three_files_are_three_cards_and_do_not_wait(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    files = []
    for name in ("a.txt", "b.txt", "c.txt"):
        path = tmp_path / name
        path.write_text("old-" + name, encoding="utf-8")
        files.append(path)
    session_id = await _session(facade, writer, str(tmp_path))
    thread = _thread(tmp_path, session_id)
    agent = agents[0]
    seen_early = {}

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        for index, target in enumerate(files):
            call_id = f"f{index}"
            _call(thread, "fs_write_file", call_id, {"path": str(target)})
            await backend.write_bytes(str(target), f"new-{index}".encode())
            _output(thread, call_id, {"ok": True})
            await _wait_completed(writer, call_id)
            seen_early[call_id] = _diff_blocks(_completed(writer, call_id))

    agent.during = during
    await _prompt(facade, session_id)
    for index, target in enumerate(files):
        call_id = f"f{index}"
        assert seen_early[call_id] == [
            {
                "type": "diff",
                "path": str(target),
                "oldText": "old-" + target.name,
                "newText": f"new-{index}",
            }
        ]
        assert target.read_text(encoding="utf-8") == f"new-{index}"
    assert "session/request_permission" not in _methods(writer)
    assert "fs/write_text_file" not in _methods(writer)
    kinds = []
    for item in _updates(writer):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kinds.append(block.get("type"))
    assert "terminal" not in kinds
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_oversize_text_is_a_short_block_and_the_turn_lives(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    target = tmp_path / "big.txt"
    old = "a" * _LIMIT
    new = "b" * (_LIMIT + 1)
    target.write_text(old, encoding="utf-8")
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "big", {"path": str(target)})
        await backend.write_bytes(str(target), new.encode())
        _output(path, "big", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    update = _completed(writer, "big")
    assert update["status"] == "completed"
    assert _diff_blocks(update) == []
    assert update["content"][0] == _preview({"ok": True})
    block = update["content"][1]
    assert block["type"] == "content"
    assert block["content"]["type"] == "text"
    text = block["content"]["text"]
    assert str(target) in text
    assert str(_LIMIT) in text
    assert str(_LIMIT + 1) in text
    assert target.read_text(encoding="utf-8") == new
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_unknown_pair_completes_the_card_without_a_diff(tmp_path):
    disk = UnreadableDisk()
    facade, writer, agents = _open(tmp_path, disk)
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]
    target = str(tmp_path / "secret.txt")

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "w1", {"path": target})
        await backend.write_bytes(target, "текст".encode())
        _output(path, "w1", {"ok": True})
        _call(path, "fs_read_file", "r1", {"path": target})
        _output(path, "r1", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    written = _completed(writer, "w1")
    assert written["status"] == "completed"
    assert written["content"] == [_preview({"ok": True})]
    assert _diff_blocks(written) == []
    read = _completed(writer, "r1")
    assert read["status"] == "completed"
    assert read["content"] == [_preview({"ok": True})]
    assert _diff_blocks(read) == []
    assert disk.files[target] == "текст".encode()
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_binary_write_completes_without_a_diff(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    target = tmp_path / "blob.bin"
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "bin", {"path": str(target)})
        await backend.write_bytes(str(target), b"\xff")
        _output(path, "bin", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    update = _completed(writer, "bin")
    assert update["status"] == "completed"
    assert update["content"] == [_preview({"ok": True})]
    assert _diff_blocks(update) == []
    assert target.read_bytes() == b"\xff"
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_failed_write_is_not_reported_as_a_diff(tmp_path):
    class FullDisk(MemoryDisk):
        async def write_bytes(self, path, data):
            del path, data
            raise OSError("full")

    disk = FullDisk()
    facade, writer, agents = _open(tmp_path, disk)
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]
    target = str(tmp_path / "note.txt")

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "w1", {"path": target})
        try:
            await backend.write_bytes(target, b"x")
        except OSError:
            pass
        _output(path, "w1", {"ok": False})

    agent.during = during
    await _prompt(facade, session_id)
    update = _completed(writer, "w1")
    assert update["status"] == "failed"
    assert update["content"] == [_preview({"ok": False})]
    assert _diff_blocks(update) == []
    assert target not in disk.files
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_relative_path_is_absolute_and_read_stays_on_disk(tmp_path):
    disk = MemoryDisk()
    facade, writer, agents = _open(tmp_path, disk)
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "w1", {"path": "note.txt"})
        await backend.write_bytes("note.txt", "на диске".encode())
        assert await backend.read_bytes("note.txt") == "на диске".encode()
        await backend.listdir(str(tmp_path))
        _output(path, "w1", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    block = _diff_blocks(_completed(writer, "w1"))[0]
    assert block["path"] == str(tmp_path / "note.txt")
    assert block["oldText"] is None
    assert block["newText"] == "на диске"
    assert disk.files["note.txt"] == "на диске".encode()
    assert disk.listed == [str(tmp_path)]
    await facade.shutdown()


async def test_session_wrapper_does_not_install_the_edit_journal(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    await _session(facade, writer, str(tmp_path))
    backend = agents[0].factory_kwargs["os_backend"]
    assert type(backend).__name__ != "TrackingFsBackend"
    assert backend._inner is disk
    assert "agent_edits" not in type(backend).__module__
    await facade.shutdown()


async def test_exact_ceiling_stays_a_diff(tmp_path):
    disk = LocalFsBackend()
    facade, writer, agents = _open(tmp_path, disk)
    target = tmp_path / "edge.txt"
    old = "a" * _LIMIT
    new = "b" * _LIMIT
    target.write_text(old, encoding="utf-8")
    session_id = await _session(facade, writer, str(tmp_path))
    path = _thread(tmp_path, session_id)
    agent = agents[0]

    async def during():
        backend = agent.factory_kwargs["os_backend"]
        _call(path, "fs_write_file", "edge", {"path": str(target)})
        await backend.write_bytes(str(target), new.encode())
        _output(path, "edge", {"ok": True})

    agent.during = during
    await _prompt(facade, session_id)
    assert _diff_blocks(_completed(writer, "edge")) == [
        {
            "type": "diff",
            "path": str(target),
            "oldText": old,
            "newText": new,
        }
    ]
    await facade.shutdown()


async def test_oversize_bodies_stay_out_of_the_log():
    disk = MemoryDisk()
    old = "a" * (_LIMIT + 1)
    new = "b" * (_LIMIT + 2)
    disk.files["/work/big.txt"] = old.encode()
    log = DiffLog()
    backend = DiffBackend(disk, log, cwd="/work", call_id=_call_id("big"))
    await backend.write_bytes("/work/big.txt", new.encode())
    side = log._by_call["big"]["/work/big.txt"]
    assert side.old_text is None
    assert side.new_text is None
    assert side.known is True
    assert side.old_size == _LIMIT + 1
    assert side.new_size == _LIMIT + 2
    shown = attach_diff(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "big",
            "status": "completed",
        },
        log,
    )
    text = shown["content"][0]["content"]["text"]
    assert text.startswith("/work/big.txt:")
    assert str(_LIMIT + 1) in text
    assert str(_LIMIT + 2) in text
    assert log._by_call == {}


async def test_kept_text_leaves_when_the_card_is_attached():
    disk = MemoryDisk()
    disk.files["note.txt"] = "было\n".encode()
    log = DiffLog()
    backend = DiffBackend(disk, log, cwd="/work", call_id=_call_id("w1"))
    await backend.write_bytes("note.txt", "стало\n".encode())
    shown = attach_diff(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "w1",
            "status": "completed",
            "content": [_preview({"ok": True})],
        },
        log,
    )
    assert _diff_blocks(shown) == [
        {
            "type": "diff",
            "path": "/work/note.txt",
            "oldText": "было\n",
            "newText": "стало\n",
        }
    ]
    assert log._by_call == {}


async def test_later_oversize_write_keeps_the_light_old_side():
    disk = MemoryDisk()
    disk.files["edit.txt"] = b"v0"
    log = DiffLog()
    backend = DiffBackend(disk, log, cwd="/work", call_id=_call_id("batch"))
    await backend.write_bytes("edit.txt", b"v1")
    side = log._by_call["batch"]["/work/edit.txt"]
    assert side.old_text == "v0"
    assert side.new_text == "v1"
    await backend.write_bytes("edit.txt", ("v" * (_LIMIT + 1)).encode())
    side = log._by_call["batch"]["/work/edit.txt"]
    assert side.old_text == "v0"
    assert side.new_text is None
    assert side.old_size == 2
    assert side.new_size == _LIMIT + 1
    shown = attach_diff(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "batch",
            "status": "completed",
        },
        log,
    )
    assert _diff_blocks(shown) == []
    assert shown["content"][0]["content"]["text"].startswith("/work/edit.txt:")
    assert log._by_call == {}


async def test_small_text_returns_after_an_oversize_write():
    disk = MemoryDisk()
    disk.files["edit.txt"] = b"v0"
    log = DiffLog()
    backend = DiffBackend(disk, log, cwd="/work", call_id=_call_id("batch"))
    await backend.write_bytes("edit.txt", b"v1")
    await backend.write_bytes("edit.txt", ("v" * (_LIMIT + 1)).encode())
    await backend.write_bytes("edit.txt", b"v2")
    shown = attach_diff(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "batch",
            "status": "completed",
        },
        log,
    )
    assert _diff_blocks(shown) == [
        {
            "type": "diff",
            "path": "/work/edit.txt",
            "oldText": "v0",
            "newText": "v2",
        }
    ]
    assert disk.files["edit.txt"] == b"v2"
    assert log._by_call == {}


def test_lost_text_under_the_limit_is_not_sent():
    log = DiffLog()
    log._by_call["batch"] = {
        "/work/edit.txt": _Side(None, None, 2, 2, True),
    }
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "batch",
        "status": "completed",
    }
    assert attach_diff(update, log) == update
    assert log._by_call == {}
