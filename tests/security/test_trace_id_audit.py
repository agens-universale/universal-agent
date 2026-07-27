import json
from types import SimpleNamespace

from security.path_policy import audit_policy_decision


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
