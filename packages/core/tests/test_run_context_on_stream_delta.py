import pytest
from gc_core import run_context as rc
from gc_testkit import as_coro
from gc_testkit.settings import make_settings


@pytest.fixture
def minimal_settings(monkeypatch):
    monkeypatch.setattr(
        rc,
        "abuild_tools_data",
        as_coro(lambda cfg, on_progress=None, **_kwargs: ({}, [], [], {})),
    )
    return make_settings({"agent.model": "M", "logging.save_logs": False})


async def test_on_stream_delta_defaults_none(minimal_settings):
    ctx = await rc.acreate_run_context(minimal_settings)
    assert ctx.on_stream_delta is None


async def test_on_stream_delta_is_stored(minimal_settings):
    cb = lambda k, t: None
    ctx = await rc.acreate_run_context(minimal_settings, on_stream_delta=cb)
    assert ctx.on_stream_delta is cb
