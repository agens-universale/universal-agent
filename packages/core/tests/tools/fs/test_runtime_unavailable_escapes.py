"""Отказ среды выходит из файлового тула, а не маскируется ошибкой файла."""

import json
import logging

import pytest
from gc_core.backends.runtime_probe import RuntimeUnavailable
from gc_core.execution_workspace import build_execution_workspace
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_core.tools.fs.list_files import ListFiles
from gc_core.tools.fs.literal_edit_common import (
    encode_and_write,
    load_text_source,
)
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.harbor_fakes import FakeBackend, bind_os_backend
from gc_testkit.settings import fake_ctx

_DOWN = RuntimeUnavailable(stderr="docker down")
_DIR = {"/work/.keep": "", "/work/f.txt": "hello\n"}


class _Boom(FakeBackend):
    """Один метод бэкенда отвечает отказом среды."""

    def __init__(self, files=None, *, op: str):
        super().__init__(files)
        self.op = op
        self.wrote = False

    def _dead(self):
        raise _DOWN

    async def makedirs(self, p):
        if self.op == "mkdir":
            self._dead()
        await super().makedirs(p)

    async def copy(self, src, dst):
        if self.op == "copy":
            self._dead()
        await super().copy(src, dst)

    async def write_bytes(self, p, data):
        self.wrote = True
        if self.op == "write":
            self._dead()
        await super().write_bytes(p, data)

    async def getsize(self, p):
        if self.op == "stat":
            self._dead()
        return await super().getsize(p)

    async def read_bytes(self, p):
        if self.op == "read":
            self._dead()
        return await super().read_bytes(p)

    async def listdir(self, p):
        if self.op == "list":
            self._dead()
        return await super().listdir(p)

    async def iter_walk_entries(self, *args, **kwargs):
        if self.op == "walk":
            self._dead()
        async for entry in super().iter_walk_entries(*args, **kwargs):
            yield entry


def _tool(cls, backend):
    return bind_os_backend(cls.__new__(cls), backend)


def _raises(cls, backend, **kwargs):
    with pytest.raises(RuntimeUnavailable, match="Runtime connection failed"):
        _tool(cls, backend)(**kwargs)


def test_write_mkdir_backup_and_write_escape():
    _raises(
        WriteToFile,
        _Boom(op="mkdir"),
        path="/missing/f.txt",
        content="x",
        make_dirs=True,
    )
    backend = _Boom(_DIR, op="copy")
    _raises(WriteToFile, backend, path="/work/f.txt", content="x", backup=True)
    assert backend.wrote is False
    backend = _Boom(_DIR, op="write")
    _raises(WriteToFile, backend, path="/work/f.txt", content="x")
    assert "/work/f.txt" in backend.files
    assert backend.files["/work/f.txt"] == "hello\n"


def test_write_journal_does_not_continue(monkeypatch):
    monkeypatch.setattr(
        "gc_core.agent_edits.recorder.is_recording", lambda _ctx: True
    )
    backend = _Boom(_DIR, op="stat")
    _raises(WriteToFile, backend, path="/work/f.txt", content="x")
    assert backend.wrote is False
    assert backend.files["/work/f.txt"] == "hello\n"


def test_append_mkdir_write_and_journal_escape(monkeypatch):
    _raises(
        AppendToFile,
        _Boom(op="mkdir"),
        path="/missing/f.txt",
        content="x",
        create_if_missing=True,
        make_dirs=True,
    )
    backend = _Boom({"/work/.keep": ""}, op="write")
    _raises(
        AppendToFile,
        backend,
        path="/work/new.txt",
        content="x",
        create_if_missing=True,
    )
    assert backend.wrote is True
    assert "new.txt" not in "".join(backend.files)
    monkeypatch.setattr(
        "gc_core.agent_edits.recorder.is_recording", lambda _ctx: True
    )
    backend = _Boom(_DIR, op="read")
    _raises(AppendToFile, backend, path="/work/f.txt", content="x")
    assert backend.files["/work/f.txt"] == "hello\n"


def test_list_and_name_search_escape():
    _raises(ListFiles, _Boom(_DIR, op="list"), path="/work")
    _raises(FindFilesByName, _Boom(_DIR, op="walk"), path="/work", query="f")


def test_find_in_files_walk_and_scan_escape():
    _raises(FindInFiles, _Boom(_DIR, op="walk"), path="/work", pattern="hello")
    _raises(FindInFiles, _Boom(_DIR, op="read"), path="/work", pattern="hello")


def test_literal_edit_stat_read_and_write_escape():
    import asyncio

    stat_tool = _tool(ApplyFileEdits, _Boom(_DIR, op="stat"))
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(
            load_text_source(
                stat_tool,
                "/work/f.txt",
                operation="edit",
                encoding=None,
                expected_file_sha256=None,
            )
        )
    read_tool = _tool(ApplyFileEdits, _Boom(_DIR, op="read"))
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(
            load_text_source(
                read_tool,
                "/work/f.txt",
                operation="edit",
                encoding=None,
                expected_file_sha256=None,
            )
        )
    write_tool = _tool(ApplyFileEdits, _Boom(_DIR, op="write"))
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(
            encode_and_write(write_tool, "/work/f.txt", "bye\n", "utf-8")
        )


def test_apply_and_replace_backup_escape():
    edit = [{"search": "hello", "replace": "bye"}]
    backend = _Boom(_DIR, op="copy")
    _raises(
        ApplyFileEdits,
        backend,
        path="/work/f.txt",
        edits=edit,
        backup=True,
    )
    assert backend.files["/work/f.txt"] == "hello\n"
    backend = _Boom(_DIR, op="copy")
    _raises(
        SearchAndReplace,
        backend,
        path="/work/f.txt",
        search="hello",
        replace="bye",
        backup=True,
    )
    assert backend.files["/work/f.txt"] == "hello\n"


def test_ordinary_oserror_stays_a_file_error():
    class _Disk(FakeBackend):
        async def write_bytes(self, p, data):
            raise OSError("disk full")

    env = json.loads(
        _tool(WriteToFile, _Disk(_DIR))(path="/work/f.txt", content="x")
    )
    assert env["error"]["type"] == "write_failed"
    assert "disk full" in env["error"]["message"]


def _tools_data(name, fabric):
    return {
        name: {
            "tool_fabric": fabric,
            "spec": {
                "type": "function",
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
            "description": "",
            "injected_fields": [],
        }
    }


async def test_apply_tool_reports_runtime_down_not_internal_error():
    from gc_core.tools import aapply_tool

    backend = _Boom(_DIR, op="write")
    # Корень — каталог фиктивной среды: иначе охрана записи видит /work
    # вне cwd хоста и отменяет вызов, не дойдя до отказа среды.
    ctx = fake_ctx(
        {"launch_root": "/work"},
        os_backend=backend,
        logger=logging.getLogger("test_runtime_unavailable"),
    )
    ctx.workspace = build_execution_workspace(backend, ctx)
    raw = await aapply_tool(
        tool_name="fs_write_file",
        arguments=json.dumps({"path": "/work/f.txt", "content": "x"}),
        tools_data_=_tools_data("fs_write_file", WriteToFile),
        ctx=ctx,
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "execution_failed"
    assert payload["error"]["message"].startswith("Runtime connection failed.")
    assert "turn_finish" in payload["error"]["message"]
    assert "guidance" not in payload
    assert payload.get("timed_out") is False
