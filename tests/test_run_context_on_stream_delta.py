import pytest
import run_context as rc


@pytest.fixture
def minimal_config_dict(monkeypatch):
    monkeypatch.setattr(
        rc,
        "build_tools_data",
        lambda cfg, on_progress=None: ({}, [], [], {}),
    )
    return {"model": "M"}


def test_on_stream_delta_defaults_none(minimal_config_dict):
    ctx = rc.create_run_context(minimal_config_dict)
    assert ctx.on_stream_delta is None


def test_on_stream_delta_is_stored(minimal_config_dict):
    cb = lambda k, t: None
    ctx = rc.create_run_context(minimal_config_dict, on_stream_delta=cb)
    assert ctx.on_stream_delta is cb
