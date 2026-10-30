"""Контекстные хуки плагинов в настоящем цикле агента (спека §7).

Через публичный вход ``AgentExecutor.arun``: что дошло до модели, сколько
раз запускался хук и что увидел пользователь.
"""

import json
import shlex
import sys
from types import SimpleNamespace

import pytest
from gc_core.agent_executor import AgentExecutor
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)

PY = sys.executable
UNTRUSTED_NOTICE = (
    "Хуки плагина plug из конфига проекта не запускаются. Чтобы "
    "включить, установите плагин командой `gc-plugin install` или "
    "добавьте его в личный конфиг"
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


@pytest.fixture(autouse=True)
def _fresh_notices(monkeypatch):
    from gc_core.plugins import catalog, hooks
    from gc_core.runtime_notices import clear_runtime_notices

    catalog.reset_plugin_cache()
    monkeypatch.setattr(hooks, "_notified", set())
    clear_runtime_notices()
    yield
    clear_runtime_notices()


def _finish_turn(call_id="c1"):
    return [
        assistant_tool_call(
            "turn_finish",
            {
                "solution_type": "direct_answer",
                "task_report": "ok",
                "handoff": "{}",
            },
            call_id=call_id,
        ),
        judge_accept(),
    ]


def _two_step_turn():
    """Вызов калькулятора, затем финал: два запроса к модели в одном ходе."""
    return [
        assistant_tool_call(
            "exec_calculator", {"expression": "1 + 1"}, call_id="c0"
        ),
        *_finish_turn(),
    ]


def _py(code: str) -> str:
    return f"{PY} -c {shlex.quote(code)}"


def _logging(log, text="", code=0):
    """Хук: пишет событие и source в ``log``, печатает ``text``."""
    return _py(
        "import json, sys\n"
        "e = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write("
        "e['hook_event_name'] + ':' + str(e.get('source', '')) + '\\n')\n"
        f"sys.stdout.write({text!r})\n"
        f"sys.stderr.write('why-{code}')\n"
        f"sys.exit({code})\n"
    )


def _plugin(tmp_path, name, hooks):
    root = tmp_path / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name})
    )
    (root / "hooks").mkdir()
    spec = {
        event: [{"hooks": [{"type": "command", "command": command}]}]
        for event, command in hooks.items()
    }
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": spec}))
    return root


def _loaded(tmp_path, monkeypatch, roots, config=(), **runtime):
    from gc_core.plugins import catalog

    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{
            "plugins.paths": [str(r) for r in roots],
            "plugins.use_installed": False,
            **dict(config),
        },
    )
    if runtime:
        cfg = cfg.with_runtime(**runtime)
    return catalog.with_plugin_snapshot(cfg, clone=False)


async def _ctx(tmp_path, monkeypatch, roots, config=(), **runtime):
    from gc_core import run_context as rc

    return await rc.acreate_run_context(
        _loaded(tmp_path, monkeypatch, roots, config, **runtime)
    )


def _requests(backend):
    return [json.dumps(c, ensure_ascii=False) for c in backend.calls]


def _lines(log):
    return log.read_text().splitlines() if log.exists() else []


def _notice_codes():
    from gc_core.runtime_notices import collect_runtime_notices

    return [n.code for n in collect_runtime_notices()]


async def test_plain_text_hooks_reach_model(tmp_path, monkeypatch):
    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path,
        "plug",
        {
            "SessionStart": _logging(log, "BOOT-TEXT"),
            "UserPromptSubmit": _logging(log, "PROMPT-TEXT"),
        },
    )
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(["привет"], thread_id="t1")

    first = _requests(backend)[0]
    assert "BOOT-TEXT" in first and "PROMPT-TEXT" in first
    assert _lines(log) == ["UserPromptSubmit:", "SessionStart:startup"]


async def test_user_prompt_hook_and_command_run_on_first_turn(
    tmp_path, monkeypatch
):
    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path, "plug", {"UserPromptSubmit": _logging(log, "CTX")}
    )
    (root / "commands").mkdir()
    (root / "commands" / "hi.md").write_text("SAY-HI $ARGUMENTS\n")
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(["/plug:hi there"], thread_id="t1")

    assert _lines(log) == ["UserPromptSubmit:"]
    assert "SAY-HI there" in _requests(backend)[0]


async def test_user_prompt_hook_sees_request_before_technical_tail(
    tmp_path, monkeypatch
):
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )

    seen = tmp_path / "prompt.txt"
    hook = _py(
        "import json, sys\n"
        f"open({str(seen)!r}, 'a').write(json.load(sys.stdin)['prompt'])\n"
    )
    root = _plugin(tmp_path, "plug", {"UserPromptSubmit": hook})
    install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(
        ["привет @a.py", wrap_technical_user_content("file a.py")],
        thread_id="t1",
    )

    assert seen.read_text() == "привет @a.py"


async def test_session_start_with_output_runs_once_per_context(
    tmp_path, monkeypatch
):
    log = tmp_path / "hooks.log"
    root = _plugin(tmp_path, "plug", {"SessionStart": _logging(log, "BOOT")})
    backend = install(
        monkeypatch, ScriptedBackend(_two_step_turn() + _finish_turn("c2"))
    )
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")
    await AgentExecutor(ctx).arun(["два"], thread_id="t1")

    assert _lines(log) == ["SessionStart:startup"]
    requests = _requests(backend)
    assert all("BOOT" in requests[i] for i in (0, 1, 3))


async def test_session_start_reruns_after_mid_turn_compaction(
    tmp_path, monkeypatch
):
    from gc_core import run_summary_coordinator as rsc

    log = tmp_path / "hooks.log"
    root = _plugin(tmp_path, "plug", {"SessionStart": _logging(log, "BOOT")})
    backend = install(monkeypatch, ScriptedBackend(_two_step_turn()))
    compacted = []

    def hits(*, state, **_kw):
        tool_done = "exec_calculator" in json.dumps(state.messages)
        return tool_done and not compacted

    async def compact(**kw):
        compacted.append(True)
        state = kw["state"]
        state.messages = [
            *(m for m in state.messages if m.get("role") == "system"),
            {
                "id": "summary",
                "role": "user",
                "content": '<technical type="dialog_summary">S</technical>',
            },
        ]

    monkeypatch.setattr(rsc, "exceeded_summary_triggers", hits)
    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    # Порог включает проверку; срабатывание решает подмена выше.
    trigger = {"summary.chat_completions_summary_trigger_chars": 10**9}
    ctx = await _ctx(tmp_path, monkeypatch, [root], trigger.items())
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")

    assert compacted == [True]
    assert _lines(log) == ["SessionStart:startup", "SessionStart:compact"]
    assert _requests(backend)[1].count("BOOT") == 1


async def test_session_start_without_output_runs_once_per_turn(
    tmp_path, monkeypatch
):
    log = tmp_path / "hooks.log"
    root = _plugin(tmp_path, "plug", {"SessionStart": _logging(log)})
    install(
        monkeypatch, ScriptedBackend(_two_step_turn() + _finish_turn("c2"))
    )
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")
    await AgentExecutor(ctx).arun(["два"], thread_id="t1")

    assert _lines(log) == ["SessionStart:startup"] * 2


async def test_plugin_added_mid_thread_gets_own_session_start(
    tmp_path, monkeypatch
):
    log_a, log_b = tmp_path / "a.log", tmp_path / "b.log"
    root_a = _plugin(
        tmp_path, "a", {"SessionStart": _logging(log_a, "CTX-OF-A")}
    )
    root_b = _plugin(
        tmp_path, "b", {"SessionStart": _logging(log_b, "CTX-OF-B")}
    )
    backend = install(
        monkeypatch, ScriptedBackend(_finish_turn() + _finish_turn("c2"))
    )
    ctx = await _ctx(tmp_path, monkeypatch, [root_a])
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")
    ctx = await _ctx(tmp_path, monkeypatch, [root_a, root_b])
    await AgentExecutor(ctx).arun(["два"], thread_id="t1")

    assert _lines(log_a) == ["SessionStart:startup"]
    assert _lines(log_b) == ["SessionStart:startup"]
    requests = _requests(backend)
    assert "CTX-OF-B" not in requests[0]
    assert "CTX-OF-A" in requests[2] and "CTX-OF-B" in requests[2]


@pytest.mark.parametrize(
    "names", [("my.plugin", "my_plugin"), ("плагин", "модуль")]
)
async def test_session_start_markers_do_not_collide(
    tmp_path, monkeypatch, names
):
    first, second = names
    root_a = _plugin(
        tmp_path, first, {"SessionStart": _py("print('CTX-ONE')")}
    )
    root_b = _plugin(
        tmp_path, second, {"SessionStart": _py("print('CTX-TWO')")}
    )
    backend = install(
        monkeypatch, ScriptedBackend(_finish_turn() + _finish_turn("c2"))
    )
    ctx = await _ctx(tmp_path, monkeypatch, [root_a])
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")
    ctx = await _ctx(tmp_path, monkeypatch, [root_a, root_b])
    await AgentExecutor(ctx).arun(["два"], thread_id="t1")

    second_turn = _requests(backend)[2]
    assert second_turn.count("CTX-ONE") == 1
    assert second_turn.count("CTX-TWO") == 1


async def test_tool_and_stop_hooks_never_run(tmp_path, monkeypatch):
    from gc_core.plugins.manifest import load_plugin

    log = tmp_path / "hooks.log"
    events = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")
    root = _plugin(
        tmp_path, "plug", {event: _logging(log, code=2) for event in events}
    )
    install(monkeypatch, ScriptedBackend(_two_step_turn()))
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")

    assert _lines(log) == []
    assert load_plugin(root).hook_problems == tuple(
        f"хуки {event} будут поддержаны после middleware" for event in events
    )


async def test_untrusted_plugin_hooks_are_skipped_with_notice(
    tmp_path, monkeypatch
):
    from gc_core import run_context as rc

    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path,
        "plug",
        {
            "SessionStart": _logging(log, "BOOT"),
            "UserPromptSubmit": _logging(log, "CTX"),
        },
    )
    install(monkeypatch, ScriptedBackend(_finish_turn()))
    loaded = _loaded(
        tmp_path,
        monkeypatch,
        [root],
        untrusted_plugin_entries=frozenset({str(root)}),
    )
    ctx = await rc.acreate_run_context(loaded)
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")

    assert _lines(log) == []
    assert loaded.runtime.notices.count(UNTRUSTED_NOTICE) == 1


async def test_exit_code_2_user_prompt_gives_reason_to_model(
    tmp_path, monkeypatch
):
    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path, "plug", {"UserPromptSubmit": _logging(log, code=2)}
    )
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await _ctx(tmp_path, monkeypatch, [root], is_tui=True)
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")

    assert "[plugin plug] why-2" in _requests(backend)[0]
    assert _notice_codes() == []


async def test_exit_code_2_session_start_is_failure(tmp_path, monkeypatch):
    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path, "plug", {"SessionStart": _logging(log, "BOOT", code=2)}
    )
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await _ctx(tmp_path, monkeypatch, [root], is_tui=True)
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")

    first = _requests(backend)[0]
    assert "BOOT" not in first and "why-2" not in first
    assert _notice_codes() == ["plugin_hook_failed"]


async def test_hook_context_is_truncated_to_limit(tmp_path, monkeypatch):
    root = _plugin(
        tmp_path, "plug", {"SessionStart": _py("print('y' * 30000)")}
    )
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")

    first = _requests(backend)[0]
    assert "y" * 10_000 in first and "y" * 10_001 not in first


async def test_failure_notice_once_in_tui(tmp_path, monkeypatch):
    from gc_core.runtime_notices import collect_runtime_notices

    log = tmp_path / "hooks.log"
    root = _plugin(tmp_path, "plug", {"SessionStart": _logging(log, code=1)})
    install(monkeypatch, ScriptedBackend(_finish_turn() + _finish_turn("c2")))
    ctx = await _ctx(tmp_path, monkeypatch, [root], is_tui=True)
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")
    await AgentExecutor(ctx).arun(["два"], thread_id="t1")

    assert len(_lines(log)) == 2
    notices = collect_runtime_notices()
    assert [n.code for n in notices] == ["plugin_hook_failed"]
    assert "plug" in notices[0].message


async def test_failure_line_once_in_cli_stderr(tmp_path, monkeypatch, capsys):
    from gc_core.agent_edits.ctx import RUN_MODE_CLI

    log = tmp_path / "hooks.log"
    root = _plugin(tmp_path, "plug", {"SessionStart": _logging(log, code=1)})
    install(monkeypatch, ScriptedBackend(_finish_turn() + _finish_turn("c2")))
    ctx = await _ctx(tmp_path, monkeypatch, [root], run_mode=RUN_MODE_CLI)
    capsys.readouterr()
    await AgentExecutor(ctx).arun(["один"], thread_id="t1")
    await AgentExecutor(ctx).arun(["два"], thread_id="t1")

    err = capsys.readouterr().err
    assert len(_lines(log)) == 2
    assert [line for line in err.splitlines() if "plug" in line] == [
        "Хук SessionStart плагина plug не сработал — подробности в логе."
    ]
    assert _notice_codes() == []


@pytest.mark.parametrize("user_prompt", [True, False])
async def test_hooks_never_run_for_subagent(
    tmp_path, monkeypatch, user_prompt
):
    from gc_core.plugins.hooks import acontext_hook_messages

    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path,
        "plug",
        {
            "SessionStart": _logging(log, "BOOT"),
            "UserPromptSubmit": _logging(log, "CTX"),
        },
    )
    loaded = _loaded(tmp_path, monkeypatch, [root])
    child = SimpleNamespace(
        state_id="c",
        parent_state_id="p",
        messages=[{"role": "user", "content": "hi"}],
        plugin_hooks_ran_turn=set(),
    )
    out = await acontext_hook_messages(
        child, loaded, child.messages, user_prompt=user_prompt
    )
    assert out == []
    assert _lines(log) == []


def test_hook_flag_is_transient_in_durable_snapshot():
    from gc_core.execution.state import ExecutionState
    from gc_core.states import State

    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="test",
        use_server_subagents=False,
    )
    state.plugin_hooks_ran_turn.add("plug")

    snapshot = state.to_execution_snapshot()
    restored = ExecutionState.from_execution_snapshot(snapshot)

    assert "plugin_hooks_ran_turn" not in snapshot["data"]
    assert restored.plugin_hooks_ran_turn == set()


def test_begin_turn_resets_hook_flag():
    from gc_core.turn_boundary import begin_turn

    state = SimpleNamespace(plugin_hooks_ran_turn={"plug"})
    begin_turn(state, SimpleNamespace())
    assert state.plugin_hooks_ran_turn == set()


@pytest.mark.parametrize("backend_mode", ["chat_completions", "responses"])
async def test_context_hooks_run_in_every_backend_mode(
    tmp_path, monkeypatch, backend_mode
):
    """UserPromptSubmit — до служебных добавок, SessionStart — перед
    объявлением режима; в любом api_mode."""
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

    async def hooks(_state, _ctx, _messages, *, user_prompt):
        called.append("prompt" if user_prompt else "start")
        return []

    async def no_judge(*_a, **_kw):
        return None

    async def announce(messages):
        called.append("announce")
        return messages

    monkeypatch.setattr(pr, "aprepare_main_call", no_judge)
    monkeypatch.setattr(pr, "acontext_hook_messages", hooks)
    monkeypatch.setattr(loop, "_aannounce_mode", announce)

    await loop._aprepare_main_request(
        [{"role": "user", "content": "hi"}], "user_ask", backend_mode
    )
    assert called == ["prompt", "start", "announce"]


async def test_overflow_retry_runs_prompt_hook_and_command_once(
    tmp_path, monkeypatch
):
    """Повтор запроса после переполнения контекста — тот же запрос:
    UserPromptSubmit не запускается снова, команда не раскрывается дважды."""
    from gc_core import processing_responses as pr
    from gc_core import run_summary_coordinator as rsc

    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path, "plug", {"UserPromptSubmit": _logging(log, "CTX")}
    )
    (root / "commands").mkdir()
    (root / "commands" / "hi.md").write_text("SAY-HI $ARGUMENTS\n")
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    real = pr.aprocess_user_ask
    overflowed = []

    async def _overflow_once(messages, state, ctx):
        if not overflowed:
            overflowed.append(True)
            raise ValueError(
                "Requested token count exceeds the model's maximum context "
                "length of 131072 tokens. Please reduce the number of tokens"
            )
        return await real(messages, state, ctx)

    async def _compact(**_kw):
        return None

    monkeypatch.setattr(pr, "aprocess_user_ask", _overflow_once)
    monkeypatch.setattr(rsc, "arun_summary_compaction", _compact)
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    await AgentExecutor(ctx).arun(["/plug:hi there"], thread_id="t1")

    assert overflowed == [True]
    first = _requests(backend)[0]
    assert (first.count("SAY-HI there"), first.count("CTX")) == (1, 1)
    assert _lines(log) == ["UserPromptSubmit:"]


async def test_queued_message_after_overflow_runs_prompt_hook_and_command(
    tmp_path, monkeypatch
):
    """Сообщение из очереди, подхваченное после переполнения контекста, —
    новый запрос: UserPromptSubmit запускается для него, команда
    раскрывается."""
    from gc_core import processing_responses as pr
    from gc_core import run_summary_coordinator as rsc

    log = tmp_path / "hooks.log"
    root = _plugin(
        tmp_path, "plug", {"UserPromptSubmit": _logging(log, "CTX")}
    )
    (root / "commands").mkdir()
    (root / "commands" / "hi.md").write_text("SAY-HI $ARGUMENTS\n")
    backend = install(monkeypatch, ScriptedBackend(_finish_turn()))
    real = pr.aprocess_user_ask
    overflowed = []
    executor = None

    async def _overflow_once(messages, state, ctx):
        if not overflowed:
            overflowed.append(True)
            executor.add_message({"role": "user", "content": "/plug:hi again"})
            raise ValueError(
                "Requested token count exceeds the model's maximum context "
                "length of 131072 tokens. Please reduce the number of tokens"
            )
        return await real(messages, state, ctx)

    async def _compact(**_kw):
        return None

    monkeypatch.setattr(pr, "aprocess_user_ask", _overflow_once)
    monkeypatch.setattr(rsc, "arun_summary_compaction", _compact)
    ctx = await _ctx(tmp_path, monkeypatch, [root])
    executor = AgentExecutor(ctx)
    await executor.arun(["/plug:hi there"], thread_id="t1")

    assert overflowed == [True]
    assert "SAY-HI again" in _requests(backend)[0]
    assert _lines(log) == ["UserPromptSubmit:", "UserPromptSubmit:"]
