"""Хуки плагина в настоящем цикле агента: UserPromptSubmit на первом ходу треда."""

import json
import sys

import pytest
from gc_core.agent_executor import AgentExecutor
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    real = pr.aapply_tool

    async def _apply(**kw):
        if kw.get("tool_name") == "turn_finish":
            return await stub_turn_finish_apply(**kw)
        return await real(**kw)

    monkeypatch.setattr(pr, "aapply_tool", _apply)


def _finish_turn():
    return [
        assistant_tool_call(
            "turn_finish",
            {
                "solution_type": "direct_answer",
                "task_report": "ok",
                "handoff": "{}",
            },
            call_id="c1",
        ),
        judge_accept(),
    ]


def _plugin(tmp_path):
    """SessionStart отдаёт контекст, UserPromptSubmit пишет prompt в лог."""
    root = tmp_path / "plug"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "plug"}')
    (root / "hooks").mkdir()
    (root / "commands").mkdir()
    (root / "commands" / "hi.md").write_text("SAY-HI $ARGUMENTS\n")
    log = tmp_path / "prompts.log"
    start = root / "hooks" / "start.py"
    start.write_text(
        "import json\nprint(json.dumps({'additionalContext': 'BOOT'}))\n"
    )
    prompt = root / "hooks" / "prompt.py"
    prompt.write_text(
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.load(sys.stdin)['prompt'] + '\\n')\n"
    )
    hooks = {
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": f"{sys.executable} {start}"}
                ]
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{sys.executable} {prompt}",
                    }
                ]
            }
        ],
    }
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    return root, log


async def test_user_prompt_hook_and_command_run_on_first_turn(
    tmp_path, monkeypatch
):
    from gc_core import run_context as rc
    from gc_core.plugins import catalog

    catalog.reset_plugin_cache()
    root, log = _plugin(tmp_path)
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{"plugins.paths": [str(root)], "plugins.use_installed": False},
    )
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await rc.acreate_run_context(
        catalog.with_plugin_snapshot(cfg, clone=False)
    )
    await AgentExecutor(ctx).arun(["/plug:hi there"], thread_id="t1")

    assert log.read_text().splitlines() == ["/plug:hi there"]
    first_request = json.dumps(backend.calls[0], ensure_ascii=False)
    assert "BOOT" in first_request
    assert "SAY-HI there" in first_request


async def test_user_prompt_hook_sees_request_before_technical_tail(
    tmp_path, monkeypatch
):
    from gc_core import run_context as rc
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )
    from gc_core.plugins import catalog

    catalog.reset_plugin_cache()
    root, log = _plugin(tmp_path)
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{"plugins.paths": [str(root)], "plugins.use_installed": False},
    )
    install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await rc.acreate_run_context(
        catalog.with_plugin_snapshot(cfg, clone=False)
    )
    await AgentExecutor(ctx).arun(
        ["привет @a.py", wrap_technical_user_content("file a.py")],
        thread_id="t1",
    )

    assert log.read_text().splitlines() == ["привет @a.py"]


@pytest.mark.parametrize("backend_mode", ["chat_completions", "responses"])
async def test_session_hooks_run_in_every_backend_mode(
    tmp_path, monkeypatch, backend_mode
):
    """SessionStart и UserPromptSubmit не зависят от api_mode, как и хуки
    инструментов и Stop."""
    from types import SimpleNamespace

    from gc_core import processing_responses as pr
    from gc_core import run_context as rc

    cfg = parity_config(tmp_path, monkeypatch)
    ctx = await rc.acreate_run_context(cfg)
    state = SimpleNamespace(messages=[], parent_state_id=None, state_id="s")
    loop = pr._AgentLoop(
        state,
        ctx,
        get_full_history_messages=None,
        persist_messages_callback=None,
        pre_backend_compaction_hook=None,
        turn_end_compaction_hook=None,
        turn_closed_hook=None,
        force_compact_hook=None,
        get_pending_messages=None,
        check_stop=None,
        session_meta=None,
        mark_finalizing=None,
        mark_finish_gate_checking=None,
    )
    called = []

    async def record(name, messages):
        called.append(name)
        return messages

    async def no_judge(*_a, **_kw):
        return None

    monkeypatch.setattr(pr, "aprepare_main_call", no_judge)
    monkeypatch.setattr(
        loop, "_aapply_plugin_user_prompt", lambda m: record("prompt", m)
    )
    monkeypatch.setattr(
        loop, "_aapply_plugin_session_start", lambda m: record("start", m)
    )
    monkeypatch.setattr(
        loop, "_aannounce_mode", lambda m: record("announce", m)
    )

    await loop._aprepare_main_request(
        [{"role": "user", "content": "hi"}], "user_ask", backend_mode
    )
    assert called == ["prompt", "start", "announce"]


@pytest.mark.parametrize(
    ("reason", "expected"), [(None, ["stop"]), ("plan_proposed", [])]
)
async def test_stop_hooks_skip_plan_proposed_close(
    tmp_path, monkeypatch, reason, expected
):
    """План ждёт одобрения человека: хук Stop не продолжает ход за него."""
    from types import SimpleNamespace

    from gc_core import processing_responses as pr
    from gc_core import run_context as rc

    cfg = parity_config(tmp_path, monkeypatch)
    ctx = await rc.acreate_run_context(cfg)
    state = SimpleNamespace(messages=[], parent_state_id=None, state_id="s")
    loop = pr._AgentLoop(
        state,
        ctx,
        get_full_history_messages=None,
        persist_messages_callback=None,
        pre_backend_compaction_hook=None,
        turn_end_compaction_hook=None,
        turn_closed_hook=None,
        force_compact_hook=None,
        get_pending_messages=None,
        check_stop=None,
        session_meta=None,
        mark_finalizing=None,
        mark_finish_gate_checking=None,
    )
    called = []

    async def stop(_result):
        called.append("stop")
        return []

    monkeypatch.setattr(loop, "_aplugin_stop", stop)

    await loop._aclose_turn("{}", reason=reason)
    assert called == expected
