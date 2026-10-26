"""match_model_objects: сырой JSON карточки из тела /models|/config."""

from gc_core.model_picker import match_model_objects


def test_match_from_data_array():
    payload = {
        "data": [
            {"id": "a", "context_length": 100},
            {"id": "b", "owned_by": "x"},
        ]
    }
    assert match_model_objects(payload, "a") == [
        {"id": "a", "context_length": 100}
    ]


def test_match_from_top_level_models_and_chat_models():
    payload = {
        "models": [{"name": "local", "n_ctx": 8192}],
        "chat": {"models": [{"model": "local", "plugin_side": {"x": 1}}]},
    }
    matched = match_model_objects(payload, "local")
    assert matched == [
        {"name": "local", "n_ctx": 8192},
        {"model": "local", "plugin_side": {"x": 1}},
    ]


def test_match_exact_no_case_fold_no_prefix_strip():
    payload = {"data": [{"id": "Foo/Bar"}, {"id": "foo/bar"}]}
    assert match_model_objects(payload, "foo/bar") == [{"id": "foo/bar"}]
    assert match_model_objects(payload, "Foo/Bar") == [{"id": "Foo/Bar"}]


def test_no_match_or_empty_payload():
    assert match_model_objects(None, "a") == []
    assert match_model_objects({"data": [{"id": "b"}]}, "a") == []
    assert match_model_objects({"data": [{"id": "a"}]}, "") == []
