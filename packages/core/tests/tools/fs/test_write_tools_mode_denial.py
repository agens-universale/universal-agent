"""Отказ режима в каждом инструменте записи — ответ тула, а не исключение:
конверт режима несёт `reason` и `guidance`, поля `message` в нём нет."""

import io
import json

import pytest
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.permission_denials import UNATTENDED_TYPE
from gc_core.security.permission_store import PermissionStore
from gc_core.tools.exec.download_file import DownloadFile
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.save_base64_file import SaveBase64File
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.helpers.tool_ctx import local_ctx, with_local_ctx


@pytest.fixture(autouse=True)
def _no_console(monkeypatch):
    """Сценарий «без человека»: stdin не терминал, вопрос в консоль не идёт."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))


_DENIALS = [
    ("plan", "write_denied_by_mode"),
    ("manual", UNATTENDED_TYPE),  # человека нет — спросить некого
]
_WRITES = [
    (WriteToFile, {"content": "x"}),
    (AppendToFile, {"content": "x"}),
    (SaveBase64File, {"data": "eA=="}),
    (SearchAndReplace, {"search": "a", "replace": "b"}),
    (ApplyFileEdits, {"edits": [{"search": "a", "replace": "b"}]}),
]


def _ctx(mode, root, config=None):
    shared = SharedStateData(permission_mode=mode)
    return local_ctx(
        {"launch_root": str(root), **(config or {})},
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
    )


@pytest.mark.parametrize("mode, error_type", _DENIALS)
@pytest.mark.parametrize("tool_cls, arguments", _WRITES)
def test_mode_denial_is_a_tool_answer(
    tmp_path, mode, error_type, tool_cls, arguments
):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    tool = with_local_ctx(tool_cls, ctx=_ctx(mode, tmp_path))
    out = json.loads(tool(path=str(target), **arguments))
    assert out["ok"] is False
    assert out["error"]["type"] == error_type
    assert target.read_text(encoding="utf-8") == "a"


@pytest.mark.parametrize("mode, error_type", _DENIALS)
def test_download_mode_denial_is_a_tool_answer(tmp_path, mode, error_type):
    ctx = _ctx(
        mode,
        tmp_path,
        {
            "security.download": {
                "download_dir": "downloads",
                "max_bytes": 1_000_000,
            }
        },
    )
    tool = with_local_ctx(DownloadFile, ctx=ctx)
    out = json.loads(tool(url="https://example.com/a.pdf", filename="a.pdf"))
    assert out["ok"] is False
    assert out["error"]["type"] == error_type
    assert not (tmp_path / "downloads" / "a.pdf").exists()


def test_mode_denial_names_the_mode(tmp_path):
    """Запрет режима называет режим: лента пишет «режим <имя>: …»."""
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    tool = with_local_ctx(WriteToFile, ctx=_ctx("plan", tmp_path))
    out = json.loads(tool(path=str(target), content="x"))
    assert out["error"]["type"] == "write_denied_by_mode"
    assert out["error"]["mode"] == "plan"
