import base64
import json

from tools.fs.save_base64_file import SaveBase64File


from tests.helpers.tool_ctx import with_local_ctx

def test_save_base64_writes_file(tmp_path):
    raw = b"\x00\xffhello"
    data = base64.b64encode(raw).decode("ascii")
    path = str(tmp_path / "out.bin")
    res = json.loads(with_local_ctx(SaveBase64File)(path=path, data=data))
    assert res["ok"] is True
    assert (tmp_path / "out.bin").read_bytes() == raw
    assert res["bytes_written"] == len(raw)


def test_save_base64_parent_not_found(tmp_path):
    path = str(tmp_path / "missing" / "out.bin")
    data = base64.b64encode(b"x").decode("ascii")
    res = json.loads(with_local_ctx(SaveBase64File)(path=path, data=data))
    assert res["ok"] is False
    assert res["error"]["type"] == "parent_not_found"


def test_save_base64_parent_is_file_returns_parent_not_found(tmp_path):
    parent_file = tmp_path / "not_a_dir"
    parent_file.write_text("blocker", encoding="utf-8")
    path = str(parent_file / "child.bin")
    data = base64.b64encode(b"x").decode("ascii")
    res = json.loads(with_local_ctx(SaveBase64File)(path=path, data=data))
    assert res["ok"] is False
    assert res["error"]["type"] == "parent_not_found"
