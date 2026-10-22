"""Хуки плагина в настоящем цикле агента: UserPromptSubmit на первом ходу треда."""

import json
import sys

import pytest
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)

from gc_core.agent_executor import AgentExecutor


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    from helpers.turn_finish_apply import stub_turn_finish_apply

    from gc_core import processing_responses as pr

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
    from gc_core.plugins import runtime

    runtime.reset_plugin_cache()
    root, log = _plugin(tmp_path)
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{"plugins.paths": [str(root)], "plugins.use_installed": False},
    )
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await rc.acreate_run_context(cfg)
    await AgentExecutor(ctx).arun(["/plug:hi there"], thread_id="t1")

    assert log.read_text().splitlines() == ["/plug:hi there"]
    first_request = json.dumps(backend.calls[0], ensure_ascii=False)
    assert "BOOT" in first_request
    assert "SAY-HI there" in first_request
