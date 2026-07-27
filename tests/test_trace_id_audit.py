import json
from pathlib import Path
from types import SimpleNamespace

# Порядок импорта важен: прямой `from security.path_policy import ...` вне
# tests/security/ (там это обходит conftest-заглушка `tools`) упирается в
# предсуществующий цикл security.confirm_port -> tools.subagents.progress ->
# tools -> tools.fs.list_files -> security.read_guard -> security.confirm_port.
# Полный импорт `tools` первым разрывает цикл, не трогая исходный код.
import tools  # noqa: F401

from security.path_policy import audit_policy_decision
from summarize.summary_logging import append_summary_trace_record


class _LoggerSpy:
    def __init__(self):
        self.lines = []

    def info(self, msg, *args, **kwargs):
        self.lines.append(msg % args if args else msg)


def _security_context(logger, cfg):
    return SimpleNamespace(logger=logger, ctx=SimpleNamespace(config_dict=cfg))


def _decision_payload(logger):
    # Событие пишется как "security_policy_decision <json>" в текст сообщения.
    prefix = "security_policy_decision "
    line = next(x for x in logger.lines if x.startswith(prefix))
    return json.loads(line[len(prefix):])


class TestSecurityAudit:
    def test_absent_trace_adds_no_key(self):
        logger = _LoggerSpy()
        audit_policy_decision(
            _security_context(logger, {}),
            tool_name="exec_command",
            decision="allow",
            reason_code="ok",
        )
        assert "trace_id" not in _decision_payload(logger)

    def test_trace_is_inside_the_payload(self):
        logger = _LoggerSpy()
        audit_policy_decision(
            _security_context(logger, {"trace_id": "wf-42"}),
            tool_name="exec_command",
            decision="deny",
            reason_code="blocked",
        )
        payload = _decision_payload(logger)
        assert payload["trace_id"] == "wf-42"
        assert payload["decision"] == "deny"


class TestSummaryTrace:
    def _ctx(self, tmp_path, **cfg):
        path = tmp_path / "summary_trace_main_abc.jsonl"
        return SimpleNamespace(summary_trace_path=str(path), config_dict=cfg), path

    def test_absent_trace_adds_no_key(self, tmp_path):
        ctx, path = self._ctx(tmp_path)
        append_summary_trace_record(
            ctx, trigger_reason="chars", trace_data={"a": 1},
            newly_summarized_message_id="m1",
        )
        record = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
        assert "trace_id" not in record

    def test_trace_is_written(self, tmp_path):
        ctx, path = self._ctx(tmp_path, trace_id="wf-42")
        append_summary_trace_record(
            ctx, trigger_reason="chars", trace_data={"a": 1},
            newly_summarized_message_id="m1",
        )
        record = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
        assert record["trace_id"] == "wf-42"
