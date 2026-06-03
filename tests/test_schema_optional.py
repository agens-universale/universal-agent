import copy
import json

import pytest

from tools.arguments_validation import parse_and_validate_tool_arguments
from tools.fs.list_files import ListFiles
from tools.fs.search_and_replace import SearchAndReplace
from tools.schema_optional import (
    OPTIONAL_EMPTY_AS_OMIT_HINT,
    OPTIONAL_OMIT_HINT,
    apply_openai_strict_parameters,
    augment_optional_property_descriptions,
    normalize_optional_empty_as_omit,
    resolve_optional_property_keys,
)
from tools.skills.skills_tools import SkillsListTool

# Optional string fields that must receive augment hints in tool_description specs.
_OPTIONAL_STRING_FIELDS = (
    (SearchAndReplace, "expected_file_sha256"),
    (ListFiles, "glob"),
)


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
