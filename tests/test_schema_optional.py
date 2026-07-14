import copy
import json

import pytest

from tools.arguments_validation import (
    WrongFunctionArgumentsException,
    parse_and_validate_tool_arguments,
)
from tools.fs.apply_file_edits import ApplyFileEdits
from tools.fs.list_files import ListFiles
from tools.fs.read_file import ReadFile
from tools.fs.search_and_replace import SearchAndReplace
from tools.inject_tool_fields import apply_injected_fields_to_spec
from tools.schema_optional import (
    OPTIONAL_EMPTY_AS_OMIT_HINT,
    OPTIONAL_OMIT_HINT,
    apply_openai_strict_object,
    apply_openai_strict_parameters,
    augment_optional_property_descriptions,
    normalize_optional_empty_as_omit,
    parameters_schema_for_local_validation,
    resolve_optional_property_keys,
)
from tools.skills.skills_tools import SkillsListTool

# Optional string fields that must receive augment hints in tool_description specs.
_OPTIONAL_STRING_FIELDS = (
    (SearchAndReplace, "expected_file_sha256"),
    (ListFiles, "glob"),
)
_REASON_FIELDS = {"reason": "Short reason for calling this tool."}


def test_resolve_optional_keys_non_strict():
    spec = SearchAndReplace.tool_description()
    params = spec["parameters"]
    optional = set(resolve_optional_property_keys(params))
    assert "expected_file_sha256" in optional
    assert "path" not in optional


def test_resolve_optional_keys_after_strict():
    spec = copy.deepcopy(SearchAndReplace.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    params = spec["parameters"]
    assert set(params["required"]) == set(params["properties"].keys())
    assert "expected_file_sha256" in params["x-optional-keys"]
    assert "path" not in params["x-optional-keys"]


def test_parameters_schema_for_local_validation_non_strict_identity():
    params = SearchAndReplace.tool_description()["parameters"]
    assert parameters_schema_for_local_validation(params) is params


def test_parameters_schema_for_local_validation_semantic_required():
    spec = copy.deepcopy(ReadFile.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    params = spec["parameters"]
    api_required = list(params["required"])
    assert set(api_required) == set(params["properties"].keys())

    validation = parameters_schema_for_local_validation(params)
    assert validation is not params
    assert params["required"] == api_required
    assert "path" in validation["required"]
    for key in ("char_offset", "char_limit", "offset", "limit"):
        assert key not in validation["required"]
        assert key in params["x-optional-keys"]


def test_parameters_schema_for_local_validation_nested_optional():
    nested = {
        "type": "object",
        "properties": {
            "must": {"type": "string"},
            "opt": {"type": "string"},
        },
        "required": ["must"],
        "additionalProperties": False,
    }
    apply_openai_strict_object(nested)
    params = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "item": nested,
        },
        "required": ["path", "item"],
        "additionalProperties": False,
        "x-openai-strict": True,
        "x-optional-keys": [],
    }
    validation = parameters_schema_for_local_validation(params)
    assert validation["required"] == ["path", "item"]
    assert validation["properties"]["item"]["required"] == ["must"]
    assert nested["required"] == list(nested["properties"].keys())


def test_normalize_empty_optional_string():
    spec = SearchAndReplace.tool_description()
    raw = {
        "path": "/tmp/f.txt",
        "search": "a",
        "replace": "b",
        "expected_file_sha256": "",
    }
    out = normalize_optional_empty_as_omit(raw, spec["parameters"])
    assert "expected_file_sha256" not in out
    assert out["path"] == "/tmp/f.txt"


def test_parse_and_validate_strips_empty_optional():
    spec = SearchAndReplace.tool_description()
    args = parse_and_validate_tool_arguments(
        tool_name="fs_search_and_replace",
        arguments={
            "path": "/tmp/f.txt",
            "search": "a",
            "replace": "b",
            "expected_file_sha256": "",
        },
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert "expected_file_sha256" not in args


def test_strict_schema_parse_strips_empty_optional(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello", encoding="utf-8")
    spec = copy.deepcopy(SearchAndReplace.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    args = parse_and_validate_tool_arguments(
        tool_name="fs_search_and_replace",
        arguments={
            "path": str(p),
            "search": "hello",
            "replace": "hi",
            "dry_run": True,
            "expected_matches": 1,
            "start_line": 1,
            "end_line": 1,
            "occurrence": "first",
            "backup": False,
            "return_diff": False,
            "diff_context_lines": 3,
            "max_diff_chars": 12000,
            "expected_file_sha256": "",
        },
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert "expected_file_sha256" not in args


def test_strict_read_file_omits_optional_ok():
    spec = copy.deepcopy(ReadFile.tool_description())
    apply_injected_fields_to_spec(spec, _REASON_FIELDS)
    apply_openai_strict_parameters(spec["parameters"])
    assert set(spec["parameters"]["required"]) == set(spec["parameters"]["properties"].keys())

    args = parse_and_validate_tool_arguments(
        tool_name="fs_read_file",
        arguments={"reason": "inspect", "path": "/tmp/f.txt"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args == {"reason": "inspect", "path": "/tmp/f.txt"}


def test_strict_read_file_missing_reason_ok():
    """Injected reason stays in the API required list, but local validation lets it be omitted."""
    spec = copy.deepcopy(ReadFile.tool_description())
    apply_injected_fields_to_spec(spec, _REASON_FIELDS)
    apply_openai_strict_parameters(spec["parameters"])
    assert "reason" in spec["parameters"]["required"]

    args = parse_and_validate_tool_arguments(
        tool_name="fs_read_file",
        arguments={"path": "/tmp/f.txt"},
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert args == {"path": "/tmp/f.txt"}


def test_strict_apply_file_edits_omits_top_level_flags_ok():
    spec = copy.deepcopy(ApplyFileEdits.tool_description())
    apply_injected_fields_to_spec(spec, _REASON_FIELDS)
    apply_openai_strict_parameters(spec["parameters"])
    args = parse_and_validate_tool_arguments(
        tool_name="fs_apply_file_edits",
        arguments={
            "reason": "batch edit",
            "path": "/tmp/f.txt",
            "edits": [
                {
                    "search": "a",
                    "replace": "b",
                    "expected_matches": None,
                    "occurrence": None,
                    "start_line": None,
                    "end_line": None,
                }
            ],
        },
        tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert "atomic" not in args
    assert "dry_run" not in args
    assert args["path"] == "/tmp/f.txt"


def test_strict_apply_file_edits_omit_nested_start_line_fails():
    spec = copy.deepcopy(ApplyFileEdits.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    with pytest.raises(WrongFunctionArgumentsException) as exc_info:
        parse_and_validate_tool_arguments(
            tool_name="fs_apply_file_edits",
            arguments={
                "path": "/tmp/f.txt",
                "edits": [
                    {
                        "search": "a",
                        "replace": "b",
                        "expected_matches": None,
                        "occurrence": None,
                        "end_line": None,
                    }
                ],
            },
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
        )
    assert "start_line" in str(exc_info.value)


def test_strict_api_schema_keeps_full_required():
    spec = copy.deepcopy(ReadFile.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    params = spec["parameters"]
    assert set(params["required"]) == set(params["properties"].keys())
    validation = parameters_schema_for_local_validation(params)
    assert set(params["required"]) == set(params["properties"].keys())
    assert set(validation["required"]) < set(params["required"])


def test_augment_optional_description_non_strict():
    spec = copy.deepcopy(SearchAndReplace.tool_description())
    augment_optional_property_descriptions(spec, openai_strict=False)
    desc = spec["parameters"]["properties"]["expected_file_sha256"]["description"]
    assert OPTIONAL_OMIT_HINT in desc
    assert OPTIONAL_EMPTY_AS_OMIT_HINT not in desc
    assert "If omitted" not in desc


def test_augment_optional_description_strict():
    spec = copy.deepcopy(SearchAndReplace.tool_description())
    apply_openai_strict_parameters(spec["parameters"])
    augment_optional_property_descriptions(spec, openai_strict=True)
    desc = spec["parameters"]["properties"]["expected_file_sha256"]["description"]
    assert OPTIONAL_EMPTY_AS_OMIT_HINT in desc
    assert OPTIONAL_OMIT_HINT not in desc


@pytest.mark.parametrize("tool_cls,field_name", _OPTIONAL_STRING_FIELDS)
def test_key_optional_strings_augmented_non_strict(tool_cls, field_name):
    spec = copy.deepcopy(tool_cls.tool_description())
    augment_optional_property_descriptions(spec, openai_strict=False)
    desc = spec["parameters"]["properties"][field_name]["description"]
    assert OPTIONAL_OMIT_HINT in desc


def test_x_preserve_empty_string_keeps_optional_key():
    spec = copy.deepcopy(SearchAndReplace.tool_description())
    spec["parameters"]["properties"]["expected_file_sha256"][  # type: ignore[index]
        "x-preserve-empty-string"
    ] = True
    raw = {
        "path": "/tmp/f.txt",
        "search": "a",
        "replace": "b",
        "expected_file_sha256": "",
    }
    out = normalize_optional_empty_as_omit(raw, spec["parameters"])
    assert out["expected_file_sha256"] == ""


def test_search_and_replace_empty_sha_direct_call(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("abc", encoding="utf-8")
    res = json.loads(
        SearchAndReplace()(
            path=str(p),
            search="abc",
            replace="xyz",
            expected_file_sha256="",
        )
    )
    assert res["ok"] is True


def test_skills_list_empty_pack_same_as_default(tmp_path):
    import yaml
    from tools.skills.registry import SkillsRegistry

    system = tmp_path / "system"
    (system / "alpha" / "entry").mkdir(parents=True)
    (system / "alpha" / "entry" / "SKILL.md").write_text(
        '---\nid: entry\nname: entry\ndescription: ""\n---\n',
        encoding="utf-8",
    )
    (system / "alpha" / "pack.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "bootstrap_skill": "entry"}),
        encoding="utf-8",
    )
    reg = SkillsRegistry(
        system_root=str(system),
        user_root=str(tmp_path / "u"),
        project_root=str(tmp_path / "p"),
    )
    tool = SkillsListTool(reg)
    default_payload = json.loads(tool())
    empty_pack_payload = json.loads(tool(pack=""))
    assert empty_pack_payload["ok"] is True
    assert {s["id"] for s in empty_pack_payload["result"]["skills"]} == {
        s["id"] for s in default_payload["result"]["skills"]
    }
