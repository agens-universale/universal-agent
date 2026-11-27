"""Heartbeat и освобождение SDK при отмене целой activity."""

import asyncio

import pytest
from gc_core.settings import default_settings
from gc_core_temporalio import (
    AgentActivity,
    AgentActivityRequest,
    turn_activity,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment


async def test_heartbeat_covers_bootstrap_and_cancellation_closes_agent(
    monkeypatch,
):
    entered, closed = asyncio.Event(), asyncio.Event()
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: beats.append(args)

    class Agent:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.set()

        async def run(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

    async def create(settings, **_kwargs):
        assert settings.runtime.human_channel is False
        assert settings.runtime.trace_id == "tr-1"
        await asyncio.sleep(0.05)
        assert len(beats) >= 2
        return Agent()

    monkeypatch.setattr(turn_activity.AsyncAgent, "create", create)
    runner = AgentActivity(
        {"a": default_settings()},
        thread_store=object(),
        heartbeat_interval=0.01,
    )
    task = asyncio.create_task(
        env.run(
            runner.run,
            AgentActivityRequest("a", "hello", "t", trace_id="tr-1"),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    count = len(beats)
    await asyncio.sleep(0.03)
    assert len(beats) == count


@pytest.mark.parametrize(
    "turn_request",
    [
        AgentActivityRequest("missing", "hello", "t"),
        AgentActivityRequest("a", "hello", ""),
        AgentActivityRequest("a", "hello", "t", trace_id="bad\ntrace"),
    ],
)
async def test_invalid_request_is_not_retried(turn_request):
    runner = AgentActivity({"a": default_settings()}, thread_store=object())
    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(runner.run, turn_request)
    assert error.value.non_retryable


def _profile_with_plugin(tmp_path):
    plug = tmp_path / "p" / "demo"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text('{"name": "demo"}')
    (plug / "skills" / "s1").mkdir(parents=True)
    (plug / "skills" / "s1" / "SKILL.md").write_text(
        "---\nname: s1\ndescription: d\n---\nbody\n"
    )
    base = default_settings()
    plugins = base.plugins.model_copy(
        update={"paths": [str(plug)], "use_installed": False}
    )
    return base.model_copy(update={"plugins": plugins})


def test_profile_plugins_are_off_with_warning(tmp_path, caplog):
    """Плагины в durable-режиме не поддерживаются: профиль с
    ``plugins.paths`` — пустой снимок и та же строка в лог, что у воркера."""
    from gc_core.plugins.snapshot import PluginSnapshot

    caplog.set_level("WARNING")
    runner = AgentActivity(
        {"a": _profile_with_plugin(tmp_path)}, thread_store=object()
    )
    settings = runner._settings(AgentActivityRequest("a", "hi", "t1"))
    assert settings.runtime.plugins == PluginSnapshot()
    assert settings.runtime.untrusted_plugin_entries == frozenset()
    assert "Профиль a: plugins под воркером Temporal" in caplog.text


def test_prebuilt_plugin_snapshot_is_dropped(tmp_path):
    from gc_core.plugins.catalog import with_plugin_snapshot
    from gc_core.plugins.snapshot import PluginSnapshot
    from gc_core.settings import LoadedSettings

    loaded = with_plugin_snapshot(
        LoadedSettings(settings=_profile_with_plugin(tmp_path)), clone=False
    )
    assert loaded.runtime.plugins != PluginSnapshot()
    runner = AgentActivity({"a": loaded}, thread_store=object())
    settings = runner._settings(AgentActivityRequest("a", "hi", "t1"))
    assert settings.runtime.plugins == PluginSnapshot()
