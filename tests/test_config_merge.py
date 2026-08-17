"""Unit-тесты для config_modules.merge.deep_merge."""

from gc_core.config_modules.merge import deep_merge


def test_scalar_override():
    assert deep_merge({"k": 1}, {"k": 2}) == {"k": 2}


def test_scalar_addition():
    assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_dict_deep_merge():
    base = {"a": {"x": 1, "y": 2}}
    overlay = {"a": {"y": 9, "z": 3}}
    assert deep_merge(base, overlay) == {"a": {"x": 1, "y": 9, "z": 3}}


def test_list_concat_dedup():
    assert deep_merge({"l": [1, 2]}, {"l": [2, 3]}) == {"l": [1, 2, 3]}


def test_list_preserves_order_base_first():
    # Сначала элементы base, потом новые из overlay.
    result = deep_merge({"l": [1, 2, 3]}, {"l": [4, 2, 5]})
    assert result == {"l": [1, 2, 3, 4, 5]}


def test_list_dedup_within_overlay():
    # Дубли внутри overlay тоже схлопываются.
    assert deep_merge({"l": [1]}, {"l": [2, 2, 3]}) == {"l": [1, 2, 3]}


def test_type_mismatch_overlay_wins():
    # list vs string — overlay побеждает.
    assert deep_merge({"k": [1, 2]}, {"k": "str"}) == {"k": "str"}


def test_dict_vs_scalar_overlay_wins():
    # dict vs scalar — overlay побеждает.
    assert deep_merge({"k": {"a": 1}}, {"k": "str"}) == {"k": "str"}


def test_empty_overlay_returns_base():
    assert deep_merge({"a": 1}, {}) == {"a": 1}


def test_empty_base_returns_overlay():
    assert deep_merge({}, {"a": 1}) == {"a": 1}


def test_nested_lists_in_dicts():
    base = {"agent": {"stop_tools": ["a", "b"]}}
    overlay = {"agent": {"stop_tools": ["b", "c"], "max_steps": 50}}
    result = deep_merge(base, overlay)
    assert result == {"agent": {"stop_tools": ["a", "b", "c"], "max_steps": 50}}


def test_does_not_mutate_inputs():
    base = {"a": [1, 2], "b": {"x": 1}}
    overlay = {"a": [3], "b": {"y": 2}}
    base_snapshot = {"a": [1, 2], "b": {"x": 1}}
    overlay_snapshot = {"a": [3], "b": {"y": 2}}
    deep_merge(base, overlay)
    assert base == base_snapshot
    assert overlay == overlay_snapshot
