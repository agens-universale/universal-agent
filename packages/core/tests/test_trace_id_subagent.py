from pathlib import Path
from types import SimpleNamespace

from gc_core.run_context import create_run_context
from gc_core.tools.subagents.runner import SubagentRunner


def test_child_ctx_inherits_trace_id(tmp_path):
    config = {
        "logs_dump_dir": str(tmp_path),
        "save_logs": True,
        "logging_format": "jsonl",
        "console_trace": False,
        "backend_api_mode": "chat_completions",
        "completions_url": "http://localhost:1/v1/chat/completions",
        "model": "test-model",
        "launch_root": str(tmp_path),
        "trace_id": "wf-42",
        "use_local_subagents": True,
    }
    parent_ctx = create_run_context(config)
    # SubagentRunner(ctx, orchestrator_state, config_dict); _build_child_ctx
    # читает только self._ctx и self._config, поэтому state здесь — заглушка.
    runner = SubagentRunner(parent_ctx, SimpleNamespace(), config)
    child_ctx = runner._build_child_ctx("subagent_coder")

    assert child_ctx.trace_id == "wf-42"
    # Дочерний файл — тот же trace, свой run_id: именно это и группирует
    # родителя с детьми в один шаг оркестратора.
    assert child_ctx.run_id != parent_ctx.run_id
    assert "wf-42" in Path(child_ctx.logging_path).name
    assert child_ctx.run_id in Path(child_ctx.logging_path).name
