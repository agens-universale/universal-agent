import copy
import json

import pytest

from tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)
from tools.fs.append_to_file import AppendToFile
from tools.schema_optional import apply_openai_strict_parameters


from tests.helpers.tool_ctx import with_local_ctx

def test_append_success_existing_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("start", encoding="utf-8")
    res = json.loads(with_local_ctx(AppendToFile)(path=str(p), content="next"))
    assert res["ok"] is True
    assert res["created"] is False
    assert res["separator"] == "\n\n"
    assert p.read_text(encoding="utf-8") == "start\n\nnext"


def test_append_create_missing_with_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c.txt"
    res = json.loads(
        with_local_ctx(AppendToFile)(path=str(p), content="hello", create_if_missing=True, make_dirs=True, separator="")
    )
    assert res["ok"] is True
    assert res["created"] is True
    assert p.read_text(encoding="utf-8") == "hello"


def test_parse_preserves_empty_separator_non_strict():
    spec = AppendToFile.tool_description()
    args = parse_and_validate_tool_arguments(
        tool_name="fs_append_file",
        arguments={"path": "/tmp/note.txt", "content": "x", "separator": ""},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args["separator"] == ""


def test_parse_rejects_omitted_separator():
    spec = AppendToFile.tool_description()
    assert "separator" in spec["parameters"]["required"]
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="fs_append_file",
            arguments={"path": "/tmp/note.txt", "content": "x"},
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
        )


def test_parse_preserves_empty_separator_strict():
    spec = copy.deepcopy(AppendToFile.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    args = parse_and_validate_tool_arguments(
        tool_name="fs_append_file",
        arguments={
            "path": "/tmp/note.txt",
            "content": "x",
            "separator": "",
            "create_if_missing": False,
            "make_dirs": False,
            "encoding": "utf-8",
        },
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args["separator"] == ""


def test_append_empty_separator_after_validation(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("Line alpha\n", encoding="utf-8")
    spec = AppendToFile.tool_description()
    args = parse_and_validate_tool_arguments(
        tool_name="fs_append_file",
        arguments={"path": str(p), "content": "Line gamma", "separator": ""},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    res = json.loads(with_local_ctx(AppendToFile)(**args))
    assert res["ok"] is True
    assert res["separator"] == ""
    assert p.read_text(encoding="utf-8") == "Line alpha\nLine gamma"


def test_append_errors(tmp_path):
    tool = with_local_ctx(AppendToFile)
    rel = json.loads(tool(path="rel.txt", content="x"))
    assert rel["ok"] is False and rel["error"]["field"] == "path"
    miss = json.loads(tool(path=str(tmp_path / "missing.txt"), content="x"))
    assert miss["ok"] is False and miss["error"]["type"] == "not_found"
    bad_sep = json.loads(tool(path=str(tmp_path / "x.txt"), content="x", separator=123))
    assert bad_sep["ok"] is False and bad_sep["error"]["field"] == "separator"
