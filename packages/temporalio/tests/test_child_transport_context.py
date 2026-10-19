"""Вход child workflow со стороны родителя: контекст ребёнка и порядок вызовов
workflow до старта (маркеры патчей — записи истории, порядок важен для реплея)."""

from types import SimpleNamespace

import pytest

import gc_core_temporalio.child_transport as transport_module
from gc_core_temporalio.child_transport import ChildTransport


class _Stop(Exception):
    pass


class _Router:
    force_legacy = False
    breaker = SimpleNamespace(snapshot=lambda: {"open": []})

    def active_level(self):
        return "default"

    def live_level(self):
        return "auto"


def _transport(monkeypatch, events, *, patched, parent_context):
    def mark(name):
        def call(*args, **kwargs):
            events.append(name)
            return None

        return call

    def is_patched(patch_id):
        events.append(f"patched:{patch_id}")
        return patched

    async def start_child_workflow(*args, **kwargs):
        events.append("start_child_workflow")
        captured["request"] = args[1]
        raise _Stop

    captured = {}
    monkeypatch.setattr(transport_module.workflow, "patched", is_patched)
    monkeypatch.setattr(
        transport_module.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="root"),
    )
    monkeypatch.setattr(
        transport_module.workflow, "start_child_workflow", start_child_workflow
    )
    monkeypatch.setattr(
        transport_module, "capture_subagent_transfer", lambda *a: "transfer"
    )
    monkeypatch.setattr(
        transport_module,
        "public_config_snapshot",
        lambda ctx: SimpleNamespace(settings="s", runtime="r"),
    )
    monkeypatch.setattr(
        transport_module,
        "PreparedRunContext",
        SimpleNamespace(from_context=lambda *a, **k: "prepared-context"),
    )
    monkeypatch.setattr(transport_module, "replace", lambda obj, **kw: obj)
    monkeypatch.setattr(
        transport_module,
        "_light_parent_snapshot",
        lambda s: events.append("snapshot") or {},
    )
    monkeypatch.setattr(
        transport_module, "check_history_size", mark("check_history_size")
    )

    operations = SimpleNamespace(
        new_id=lambda kind: events.append("new_id") or "c1",
        child_context=parent_context,
        owner_id="owner",
        resource_owner="res",
    )
    ctx = SimpleNamespace(model_router=_Router())
    store = SimpleNamespace(owner_id="store", owner_epoch=1, _generation=2)
    transport = ChildTransport(
        ctx, "start", "prepared", store, operations, None
    )
    runner = SimpleNamespace(
        _orchestrator_state=SimpleNamespace(stop_requested=False)
    )
    return transport, runner, captured


@pytest.mark.parametrize("patched", [True, False])
@pytest.mark.parametrize(
    "parent_context", [None, {"session_id": "root-session"}]
)
async def test_child_context_and_workflow_call_order(
    monkeypatch, patched, parent_context
):
    events = []
    transport, runner, captured = _transport(
        monkeypatch, events, patched=patched, parent_context=parent_context
    )
    with pytest.raises(_Stop):
        await transport.execute(
            runner, "subagent_worker", "{}", parent_call_id="call"
        )
    expected = {
        "session_id": "root-session" if parent_context else "root",
        "parent_owner_id": "owner",
        "parent_context": parent_context,
        "tool_name": "subagent_worker",
        "frozen_level": "default",
        "force_legacy": False,
        "circuit_breaker": {"open": []},
    }
    if patched:
        expected |= {"frozen_live_level": "auto", "independent_auto": True}
    assert captured["request"].child_context == expected
    assert events == [
        "new_id",
        "patched:subagent-frozen-live-level-v1",
        "patched:subagent-independent-auto-v1",
        "snapshot",
        "check_history_size",
        "start_child_workflow",
    ]
    assert transport.active_child is None
