"""Успешная проба живости живёт runtime_probe_ttl_sec. Отказ не кэшируется."""

import asyncio

import pytest
import yaml
from gc_core.backends.factory import resolve_os_backend
from gc_core.backends.runtime_probe import (
    DEFAULT_RUNTIME_PROBE_TTL_SEC,
    _PROBE_FAILURE_DETAIL_CHARS,
    RuntimeProbeCache,
    RuntimeUnavailable,
)
from gc_core.config import load_settings
from gc_core.config_modules.agent import AgentConfig
from gc_core.config_modules.root import AppConfig
from gc_testkit.runtime_probe import probe_stdout
from gc_testkit.settings import make_settings


def _ok_run(calls: list):
    async def run(command: str, _timeout: int):
        calls.append(command)
        return type(
            "Res",
            (),
            {
                "stdout": probe_stdout(command),
                "stderr": "",
                "return_code": 0,
                "timed_out": False,
            },
        )()

    return run


def test_ttl_schema_defaults_zero_and_rejects_garbage():
    assert AgentConfig().runtime_probe_ttl_sec == DEFAULT_RUNTIME_PROBE_TTL_SEC
    assert AgentConfig(runtime_probe_ttl_sec=None).runtime_probe_ttl_sec == (
        DEFAULT_RUNTIME_PROBE_TTL_SEC
    )
    assert AgentConfig(runtime_probe_ttl_sec=0).runtime_probe_ttl_sec == 0
    assert AgentConfig(runtime_probe_ttl_sec=20).runtime_probe_ttl_sec == 20
    with pytest.raises(ValueError, match="runtime_probe_ttl_sec"):
        AgentConfig(runtime_probe_ttl_sec="abc")
    with pytest.raises(ValueError, match="runtime_probe_ttl_sec"):
        AgentConfig(runtime_probe_ttl_sec=-1)


def test_agent_config_accepts_zero_and_rejects_negative():
    assert AgentConfig().runtime_probe_ttl_sec == DEFAULT_RUNTIME_PROBE_TTL_SEC
    assert AgentConfig(runtime_probe_ttl_sec=0).runtime_probe_ttl_sec == 0
    with pytest.raises(ValueError, match="runtime_probe_ttl_sec"):
        AgentConfig(runtime_probe_ttl_sec=-1)


def test_flat_config_carries_ttl(tmp_path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.dump(
            {
                "backend": {
                    "completions_url": "http://localhost/v1/chat/completions"
                },
                "agent": {"runtime_probe_ttl_sec": 0},
            }
        ),
        encoding="utf-8",
    )
    agent = load_settings(str(cfg_path)).settings.agent
    assert agent.runtime_probe_ttl_sec == 0
    assert (
        AppConfig.model_validate({}).agent.runtime_probe_ttl_sec
        == DEFAULT_RUNTIME_PROBE_TTL_SEC
    )


def test_factory_passes_ttl_to_harbor_and_remote():
    loop = asyncio.new_event_loop()
    try:
        harbor = resolve_os_backend(
            make_settings(
                {
                    "os_backend": "harbor",
                    "agent.runtime_probe_ttl_sec": 0,
                }
            ),
            environment=object(),
            loop=loop,
        )
        remote = resolve_os_backend(
            make_settings(
                {
                    "os_backend.kind": "docker",
                    "os_backend.docker.name": "box",
                    "agent.runtime_probe_ttl_sec": 7,
                }
            )
        )
    finally:
        loop.close()
    assert harbor._runtime_probe.ttl_sec == 0
    assert remote._runtime_probe.ttl_sec == 7


def test_demo_config_documents_the_ttl():
    from gc_testkit.paths import REPO

    doc = yaml.safe_load(
        (REPO / "demo_config.yml").read_text(encoding="utf-8")
    )
    assert (
        doc["agent"]["runtime_probe_ttl_sec"] == DEFAULT_RUNTIME_PROBE_TTL_SEC
    )


async def test_success_is_reused_until_the_ttl(monkeypatch):
    calls: list[str] = []
    clock = {"t": 100.0}
    monkeypatch.setattr(
        "gc_core.backends.runtime_probe.time.monotonic", lambda: clock["t"]
    )
    cache = RuntimeProbeCache(10)
    await cache.ensure(_ok_run(calls))
    await cache.ensure(_ok_run(calls))
    assert len(calls) == 1
    clock["t"] += 10
    await cache.ensure(_ok_run(calls))
    assert len(calls) == 2


async def test_failure_is_not_cached():
    calls: list[str] = []

    async def dead(command: str, _timeout: int):
        calls.append(command)
        return type(
            "Res",
            (),
            {
                "stdout": "",
                "stderr": "down",
                "return_code": 1,
                "timed_out": False,
            },
        )()

    cache = RuntimeProbeCache(10)
    with pytest.raises(RuntimeUnavailable):
        await cache.ensure(dead)
    with pytest.raises(RuntimeUnavailable):
        await cache.ensure(dead)
    assert len(calls) == 2


async def test_zero_ttl_probes_every_call():
    calls: list[str] = []
    cache = RuntimeProbeCache(0)
    await cache.ensure(_ok_run(calls))
    await cache.ensure(_ok_run(calls))
    assert len(calls) == 2


async def test_parallel_calls_share_one_successful_probe():
    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def run(command: str, _timeout: int):
        calls.append(command)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return type(
            "Res",
            (),
            {
                "stdout": probe_stdout(command),
                "stderr": "",
                "return_code": 0,
                "timed_out": False,
            },
        )()

    cache = RuntimeProbeCache(10)

    async def go():
        await cache.ensure(run)

    first = asyncio.create_task(go())
    await started.wait()
    second = asyncio.create_task(go())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 1


def test_failed_probe_keeps_only_the_start_of_the_reply():
    """В надж и в потоки исключения — начало ответа, не весь захват."""
    blob = "banner\n" + ("x" * 5_000)
    exc = RuntimeUnavailable(
        stdout=blob, stderr="docker down\n" + ("y" * 5_000)
    )
    assert exc.stdout.startswith("banner\n")
    assert exc.stdout.endswith("...")
    assert len(exc.stdout) == _PROBE_FAILURE_DETAIL_CHARS + len("...")
    assert exc.stderr.startswith("docker down\n")
    assert len(exc.stderr) == _PROBE_FAILURE_DETAIL_CHARS + len("...")
    detail = str(exc).split("(", 1)[1].rstrip(")")
    assert detail.startswith("banner\n")
    assert len(detail) == _PROBE_FAILURE_DETAIL_CHARS + len("...")
    assert "yyyy" not in str(exc)
