import pytest
from gc_core import run_context as rc
from gc_testkit import as_coro


@pytest.fixture
def minimal_config_dict(monkeypatch):
    monkeypatch.setattr(
        rc,
        "abuild_tools_data",
        as_coro(lambda cfg, on_progress=None, **_kwargs: ({}, [], [], {})),
    )
    return {"model": "M", "save_logs": False}


async def test_on_stream_delta_defaults_none(minimal_config_dict):
    ctx = await rc.acreate_run_context(minimal_config_dict)
    assert ctx.on_stream_delta is None


async def test_on_stream_delta_is_stored(minimal_config_dict):
    cb = lambda k, t: None
    ctx = await rc.acreate_run_context(minimal_config_dict, on_stream_delta=cb)
    assert ctx.on_stream_delta is cb
