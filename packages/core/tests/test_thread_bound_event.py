"""Событие ``thread_bound``: связка «этот лог-файл ↔ этот тред».

На момент создания логгера тред ещё не разрешён (он приходит аргументом в
``run()``), поэтому в ``agent_start`` его нет, а ``RunContextFilter`` ставит
только ``run_id``. Без этого события тред виден в логе лишь косвенно — через
сообщения суммаризации, и только если она вообще срабатывала.
"""
import json
from pathlib import Path
from unittest.mock import patch

from gc_core import agent_executor as agent_executor_module
from gc_core.run_context import create_run_context


def _config(tmp_path, log_format):
    return {
        "logs_dump_dir": str(tmp_path / "logs"),
        "threads_data_path": str(tmp_path / "threads"),
        "save_logs": True,
        "logging_format": log_format,
        "console_trace": False,
        "backend_api_mode": "chat_completions",
        "completions_url": "http://localhost:1/v1/chat/completions",
        "model": "test-model",
        "launch_root": str(tmp_path),
        "max_steps": 0,
        "system_prompt_id": "v1",
        "chat_completions_system_prompt_path": str(_prompt_file(tmp_path)),
        "no_tool_call_max_count": 5,
        "verbose": False,
    }


def _prompt_file(tmp_path):
    path = tmp_path / "system_prompt.md"
    if not path.exists():
        path.write_text("SYSTEM PROMPT", encoding="utf-8")
    return path


def _run(tmp_path, log_format, **run_kwargs):
    """Прогнать run() с заглушкой вместо цикла агента; вернуть текст лога."""
    ctx = create_run_context(_config(tmp_path, log_format))

    async def _fake_run_agent(messages, state, _ctx, **_kwargs):
        state.messages += list(messages)
        return "ok"

    with patch.object(
        agent_executor_module, "arun_agent", side_effect=_fake_run_agent
    ):
        executor = agent_executor_module.AgentExecutor(ctx)
        result = executor.run(["привет"], verbose=False, **run_kwargs)

    assert result == "ok"
    for handler in ctx.logger.handlers:
        handler.flush()
    return Path(ctx.logging_path).read_text(encoding="utf-8")


def _events(text):
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class TestJsonl:
    def test_event_carries_thread_and_system_id(self, tmp_path):
        text = _run(tmp_path, "jsonl", thread_id="thr-1", system="cli")
        bound = [e for e in _events(text) if e.get("event") == "thread_bound"]
        assert len(bound) == 1
        assert bound[0]["thread_id"] == "thr-1"
        assert bound[0]["system_id"] == "cli"

    def test_event_carries_run_id_for_free(self, tmp_path):
        # run_id ставит RunContextFilter на каждую запись — дублировать его в
        # extra= не нужно, и именно он связывает событие с именем файла.
        text = _run(tmp_path, "jsonl", thread_id="thr-1", system="cli")
        bound = [e for e in _events(text) if e.get("event") == "thread_bound"][0]
        assert bound["run_id"]

    def test_no_event_without_thread(self, tmp_path):
        # gc-cli без -t: треда нет вовсе, связывать не с чем — и это не падение.
        text = _run(tmp_path, "jsonl")
        assert not [e for e in _events(text) if e.get("event") == "thread_bound"]


class TestText:
    """При logging.format: text поля из extra= не рендерятся вообще — значения
    обязаны быть в самом сообщении, иначе в стоковой конфигурации в строке
    осталось бы голое имя события."""

    def test_values_are_in_the_line_itself(self, tmp_path):
        text = _run(tmp_path, "text", thread_id="thr-1", system="cli")
        line = [ln for ln in text.splitlines() if "thread_bound" in ln]
        assert len(line) == 1
        assert "thread_id=thr-1" in line[0]
        assert "system_id=cli" in line[0]

    def test_no_event_without_thread(self, tmp_path):
        text = _run(tmp_path, "text")
        assert "thread_bound" not in text


def test_system_defaults_to_cli(tmp_path):
    # run(system=None) → system_id 'cli'; в лог уходит значение из ThreadSession,
    # а не сырой аргумент.
    text = _run(tmp_path, "jsonl", thread_id="thr-1")
    bound = [e for e in _events(text) if e.get("event") == "thread_bound"][0]
    assert bound["system_id"] == "cli"


def test_no_crash_when_logger_is_absent(tmp_path):
    """ctx без logger: run() не обязан падать из-за диагностической записи."""
    ctx = create_run_context(_config(tmp_path, "jsonl"))
    ctx.logger = None

    async def _fake_run_agent(messages, state, _ctx, **_kwargs):
        state.messages += list(messages)
        return "ok"

    with patch.object(
        agent_executor_module, "arun_agent", side_effect=_fake_run_agent
    ):
        executor = agent_executor_module.AgentExecutor(ctx)
        assert executor.run(["привет"], thread_id="thr-1", system="cli") == "ok"
