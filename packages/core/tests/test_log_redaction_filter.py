"""Секреты маскируются в САМОЙ записи лога прогона, а не только в файле.

Под Temporal (``save_logs: false``, ``propagate: true``) записи уходят в
корневой ``logging`` и в обработчики приложения (``log_handlers``) — их
форматтеры ядра не видят. Маска обязана доехать и туда.
"""

import logging

from gc_core.utils.logging_module import create_run_logger, release_run_logger
from gc_testkit.settings import make_settings

TOKEN = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.records = []

    def emit(self, record):
        self.records.append(record)
        self.lines.append(logging.Formatter("%(message)s").format(record))


def _logger(enabled, handler):
    settings = make_settings(
        {
            "logging.save_logs": False,
            "logging.propagate": True,
            "logging.redact_secrets.enabled": enabled,
        }
    )
    logger, _, _ = create_run_logger(settings, log_handlers=[handler])
    return logger


def test_foreign_and_root_handlers_get_masked_records():
    handler, root = _Capture(), _Capture()
    logging.getLogger().addHandler(root)
    try:
        logger = _logger(True, handler)
        logger.warning("Authorization: Bearer %s", TOKEN)
        logger.info("event", extra={"api_key": TOKEN, "body": f"key {TOKEN}"})
        try:
            raise RuntimeError(f"failed with {TOKEN}")
        except RuntimeError:
            logger.exception("boom")
        release_run_logger(logger)
    finally:
        logging.getLogger().removeHandler(root)
    for capture in (handler, root):
        dump = "\n".join(capture.lines) + "".join(
            str(r.__dict__) for r in capture.records
        )
        assert TOKEN not in dump
        assert len(capture.records) == 3


def test_masking_is_off_when_redaction_is_disabled():
    handler = _Capture()
    logger = _logger(False, handler)
    logger.warning("Bearer %s", TOKEN)
    release_run_logger(logger)
    assert TOKEN in handler.lines[0]


def test_structured_message_keeps_fields_and_masks_keys_for_foreign_handler():
    handler = _Capture()
    logger = _logger(True, handler)
    payload = {
        "event": "request_done",
        "api_key": "arbitrary-private-value",
        "data": {"password": "another-private-value", "count": 3},
    }
    try:
        logger.info(payload)
    finally:
        release_run_logger(logger)
    record = handler.records[0]
    assert isinstance(record.msg, dict)
    assert record.msg["event"] == "request_done"
    assert record.msg["data"]["count"] == 3
    assert "arbitrary-private-value" not in str(record.__dict__)
    assert "another-private-value" not in str(record.__dict__)
    assert payload["api_key"] == "arbitrary-private-value"
    assert payload["data"]["password"] == "another-private-value"
