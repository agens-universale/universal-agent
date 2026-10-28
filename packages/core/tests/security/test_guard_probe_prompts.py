"""Промпты судьи команд в пробе воркера читаются на loop: хранилище агента
бывает сетевым (база, S3), и синхронный мост ``run_sync`` его не дождётся."""

import asyncio

from gc_core.security.guard_probe import (
    GuardProbeRequest,
    aevaluate_guard_probes,
)
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import fake_ctx


class _NetworkStore(MemoryAgentStore):
    async def read(self, kind, name, *, project=None):
        await asyncio.sleep(0)  # «сеть»: run_sync тут отказал бы
        return await super().read(kind, name, project=project)


async def test_command_prompts_probe_awaits_a_network_store(tmp_path):
    store = _NetworkStore(
        prompts={
            "command_judge_system": "Суди.",
            "command_judge_user": "Команда: {command}",
        },
        fallback=None,
    )
    ctx = fake_ctx({}, agent_store=store)
    request = GuardProbeRequest(
        "command_prompts", "exec_command", "", str(tmp_path)
    )
    (result,) = await aevaluate_guard_probes([request], settings=ctx)
    assert result.data is not None
    assert "Суди." in str(result.data)


async def test_other_probes_keep_their_order(tmp_path):
    store = _NetworkStore(
        prompts={"command_judge_system": "S", "command_judge_user": "U"},
        fallback=None,
    )
    ctx = fake_ctx({}, agent_store=store)
    requests = [
        GuardProbeRequest("command_host", "exec_command", "ls", str(tmp_path)),
        GuardProbeRequest(
            "command_prompts", "exec_command", "", str(tmp_path)
        ),
    ]
    first, second = await aevaluate_guard_probes(requests, settings=ctx)
    assert "never_touch_paths" in first.data  # проба хоста — на своём месте
    assert second.data is not None and first.facts_digest
