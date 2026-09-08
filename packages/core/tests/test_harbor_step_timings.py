# tests/test_harbor_step_timings.py
# Pure-stdlib adapter module: a fake `processing_responses` stands in for the core.
import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_testkit.paths import HARBOR



@pytest.fixture
def timings_mod(monkeypatch):
    core = types.ModuleType("gc_core.processing_responses")

    # Заглушка повторяет ФОРМУ швов ядра: обе корутинные. Синхронные имена
    # адаптер больше не оборачивает — агентский цикл ходит в `a`-формы, и
    # обёртка над шимом портала не увидела бы ни одного вызова.
    async def _arequest_backend_canonical(messages, state, ctx, **kw):
        return "llm-result"

    core._arequest_backend_canonical = _arequest_backend_canonical

    async def _aapply_tool(*, tool_name, arguments, ctx, state, run_agent_func=None,
                           call_id=None):
        # A subagent tool calls the backend from inside the tool span.
        if tool_name == "subagent":
            await core._arequest_backend_canonical([], state, ctx)
        if tool_name == "boom":
            raise RuntimeError("tool exploded")
        return "tool-result"

    core.aapply_tool = _aapply_tool
    # Подменять надо И запись в sys.modules, И атрибут на пакете. После переезда
    # адаптер берёт ядро как `from gc_core import processing_responses`, а эта форма
    # читает АТРИБУТ пакета: стоит соседнему тесту импортировать настоящий модуль
    # раньше — атрибут уже проставлен, и одна лишь подмена sys.modules не видна.
    # В одиночку файл при этом проходит, поэтому класс ловится только полным прогоном.
    import gc_core
    monkeypatch.setitem(sys.modules, "gc_core.processing_responses", core)
    monkeypatch.setattr(gc_core, "processing_responses", core, raising=False)
    monkeypatch.syspath_prepend(str(HARBOR))
    for key in [k for k in sys.modules if k.startswith("adapters.step_timings")]:
        del sys.modules[key]
    import adapters.step_timings as mod

    monkeypatch.setattr(mod, "_installed", False)
    monkeypatch.setattr(mod, "_sinks", {})
    return mod, core


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_llm_and_tool_spans_are_recorded_per_step(timings_mod, tmp_path):
    mod, core = timings_mod
    sink = mod.StepTimings(tmp_path / "timings.jsonl")
    sink.attach()
    state = SimpleNamespace(steps=3, default_model="test-model")
    asyncio.run(core._arequest_backend_canonical([], state, None))
    asyncio.run(
        core.aapply_tool(tool_name="execute_command", arguments="", ctx=None, state=state)
    )
    sink.close()

    records = _read(tmp_path / "timings.jsonl")
    assert [(r["ev"], r["kind"], r["name"]) for r in records] == [
        ("open", "llm", "test-model"),
        ("end", "llm", "test-model"),
        ("open", "tool", "execute_command"),
        ("end", "tool", "execute_command"),
    ]
    assert {r["step"] for r in records} == {3}
    assert sink.summary()["llm_calls"] == 1
    assert sink.summary()["tool_calls"] == 1


def test_subagent_llm_is_nested_and_not_double_counted(timings_mod, tmp_path):
    mod, core = timings_mod
    sink = mod.StepTimings(tmp_path / "timings.jsonl")
    sink.attach()
    state = SimpleNamespace(steps=1, default_model="m")
    asyncio.run(core.aapply_tool(tool_name="subagent", arguments="", ctx=None, state=state))
    sink.close()

    ends = [r for r in _read(tmp_path / "timings.jsonl") if r["ev"] == "end"]
    assert [(r["kind"], r["depth"]) for r in ends] == [("llm", 1), ("tool", 0)]
    # Only the depth-0 tool span counts: the nested LLM wait is already inside it.
    assert sink.summary()["llm_calls"] == 0
    assert sink.summary()["tool_calls"] == 1


def test_failing_tool_is_recorded_and_reraised(timings_mod, tmp_path):
    mod, core = timings_mod
    sink = mod.StepTimings(tmp_path / "timings.jsonl")
    sink.attach()
    state = SimpleNamespace(steps=0, default_model="m")
    with pytest.raises(RuntimeError):
        asyncio.run(core.aapply_tool(tool_name="boom", arguments="", ctx=None, state=state))
    sink.close()

    end = _read(tmp_path / "timings.jsonl")[-1]
    assert end["ok"] is False
    assert end["error"].startswith("RuntimeError: tool exploded")


def test_detached_sink_leaves_the_core_untouched(timings_mod, tmp_path):
    mod, core = timings_mod
    sink = mod.StepTimings(tmp_path / "timings.jsonl")
    sink.attach()
    sink.close()
    state = SimpleNamespace(steps=0, default_model="m")
    assert asyncio.run(
        core.aapply_tool(tool_name="x", arguments="", ctx=None, state=state)
    ) == "tool-result"
    assert not (tmp_path / "timings.jsonl").read_text(encoding="utf-8")
