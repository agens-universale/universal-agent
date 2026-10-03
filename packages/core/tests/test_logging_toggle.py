import json

from gc_core.security.log_redaction import MASK
from gc_core.utils.logging_module import create_run_logger
from gc_testkit.settings import make_settings


def _base(tmp_path, **over):
    cfg = {
        "logs_dump_dir": str(tmp_path),
        "save_logs": True,
        "console_trace": False,
    }
    cfg.update(over)
    return make_settings(cfg)


def test_text_default_writes_dot_log_plaintext(tmp_path):
    logger, path, _ = create_run_logger(
        _base(tmp_path), run_id="r1", agent_kind="main"
    )
    assert path.endswith(".log")
    logger.info("hello world", extra={"event": "agent_start"})
    for h in logger.handlers:
        h.flush()
    content = open(path, encoding="utf-8").read()
    assert "hello world" in content
    # plaintext, not JSON
    assert not content.strip().startswith("{")


def test_jsonl_writes_dot_jsonl_events(tmp_path):
    logger, path, _ = create_run_logger(
        _base(tmp_path, logging_format="jsonl"), run_id="r2", agent_kind="main"
    )
    assert path.endswith(".jsonl")
    logger.info("msg", extra={"event": "agent_start", "foo": 1})
    for h in logger.handlers:
        h.flush()
    rec = json.loads(open(path, encoding="utf-8").readlines()[-1])
    assert rec["event"] == "agent_start"
    assert rec["foo"] == 1
    assert rec["run_id"] == "r2"
    assert rec["agent_kind"] == "main"


def test_jsonl_redaction_masks_payload(tmp_path):
    cfg = _base(
        tmp_path,
        logging_format="jsonl",
        redact_secrets={
            "enabled": True,
            "extra_keys": [],
            "extra_patterns": [],
        },
    )
    logger, path, _ = create_run_logger(cfg, run_id="r3", agent_kind="main")
    logger.info(
        "x",
        extra={
            "event": "e",
            "Authorization": "Bearer zzz",
            "note": "Bearer abcdef.ghijk-123",
        },
    )
    for h in logger.handlers:
        h.flush()
    line = open(path, encoding="utf-8").read()
    assert "abcdef.ghijk-123" not in line
    assert MASK in line


def test_text_redaction_masks_line(tmp_path):
    cfg = _base(tmp_path, redact_secrets={"enabled": True})
    logger, path, _ = create_run_logger(cfg, run_id="r4", agent_kind="main")
    logger.info("token Bearer abcdef.ghijk-123 tail", extra={"event": "e"})
    for h in logger.handlers:
        h.flush()
    content = open(path, encoding="utf-8").read()
    assert "abcdef.ghijk-123" not in content
    assert MASK in content


def test_text_no_redaction_by_default_keeps_secret(tmp_path):
    logger, path, _ = create_run_logger(
        _base(tmp_path), run_id="r5", agent_kind="main"
    )
    logger.info("Bearer keepme-1234567890", extra={"event": "e"})
    for h in logger.handlers:
        h.flush()
    assert "keepme-1234567890" in open(path, encoding="utf-8").read()
