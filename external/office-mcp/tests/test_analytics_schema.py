import pytest
from pydantic import ValidationError

from office_mcp.analytics_schema import FilterClause, parse_filter


def test_simple_clause_accepted():
    c = FilterClause.model_validate({"col": "status", "op": "==", "value": "open"})
    assert c.col == "status"
    assert c.op == "=="
    assert c.value == "open"


@pytest.mark.parametrize("op", [
    "==", "!=", ">", ">=", "<", "<=",
    "in", "not_in", "contains", "startswith", "endswith",
    "is_null", "not_null", "between",
])
def test_operator_whitelist(op):
    payload = {"col": "x", "op": op}
    if op in ("in", "not_in"):
        payload["value"] = [1, 2]
    elif op == "between":
        payload["value"] = [1, 10]
    elif op in ("is_null", "not_null"):
        pass  # no value
    elif op in ("contains", "startswith", "endswith"):
        payload["value"] = "abc"
    else:
        payload["value"] = 1
    FilterClause.model_validate(payload)


def test_unknown_operator_rejected():
    with pytest.raises(ValidationError):
        FilterClause.model_validate({"col": "x", "op": "matches", "value": "y"})


def test_in_requires_list():
    with pytest.raises(ValidationError, match="list"):
        FilterClause.model_validate({"col": "x", "op": "in", "value": "y"})


def test_between_requires_two_element_list():
    with pytest.raises(ValidationError, match="two"):
        FilterClause.model_validate({"col": "x", "op": "between", "value": [1]})


def test_is_null_does_not_require_value():
    FilterClause.model_validate({"col": "x", "op": "is_null"})


def test_parse_filter_top_level_array_is_AND():
    parsed = parse_filter([
        {"col": "a", "op": "==", "value": 1},
        {"col": "b", "op": ">", "value": 0},
    ])
    assert parsed["kind"] == "and"
    assert len(parsed["children"]) == 2


def test_parse_filter_or_clause():
    parsed = parse_filter([
        {"or": [
            {"col": "a", "op": "==", "value": 1},
            {"col": "a", "op": "==", "value": 2},
        ]}
    ])
    assert parsed["kind"] == "and"
    assert parsed["children"][0]["kind"] == "or"
    assert len(parsed["children"][0]["children"]) == 2


def test_parse_filter_empty_array():
    parsed = parse_filter([])
    assert parsed["kind"] == "and"
    assert parsed["children"] == []


from office_mcp.analytics_schema import (  # noqa: E402
    AGG_FUNCS,
    OutputSpec,
    SortSpec,
)


@pytest.mark.parametrize("func", [
    "sum", "mean", "count", "min", "max",
    "median", "std", "first", "last", "nunique",
])
def test_agg_funcs_whitelist(func):
    assert func in AGG_FUNCS


def test_sort_spec_default_order():
    s = SortSpec.model_validate({"col": "x"})
    assert s.col == "x"
    assert s.order == "asc"


def test_sort_spec_desc():
    s = SortSpec.model_validate({"col": "x", "order": "desc"})
    assert s.order == "desc"


def test_sort_spec_rejects_unknown_order():
    with pytest.raises(ValidationError):
        SortSpec.model_validate({"col": "x", "order": "ascending"})


def test_output_spec_data_string():
    o = OutputSpec.parse("data")
    assert o.kind == "data"
    assert o.path is None


def test_output_spec_default_is_data():
    o = OutputSpec.parse(None)
    assert o.kind == "data"


def test_output_spec_file_dict():
    o = OutputSpec.parse({"path": "/tmp/x.xlsx", "sheet": "Result", "overwrite": True})
    assert o.kind == "file"
    assert o.path == "/tmp/x.xlsx"
    assert o.sheet == "Result"
    assert o.overwrite is True


def test_output_spec_file_default_sheet_and_overwrite():
    o = OutputSpec.parse({"path": "/tmp/x.xlsx"})
    assert o.kind == "file"
    assert o.sheet == "Sheet1"
    assert o.overwrite is False


def test_output_spec_invalid_string_rejected():
    with pytest.raises(ValueError):
        OutputSpec.parse("file")


def test_output_spec_dict_without_path_rejected():
    with pytest.raises(ValueError):
        OutputSpec.parse({"sheet": "x"})
