import json
from pathlib import Path

from gc_core.utils.logging_module import build_run_log_paths, create_run_logger


def _close(logger):
    for h in logger.handlers:
        h.close()
    logger.handlers.clear()


class TestFileNames:
    def test_absent_trace_keeps_current_names(self, tmp_path):
        log_path, summary_path = build_run_log_paths(
            config_dict={"logs_dump_dir": str(tmp_path)},
            run_id="abc",
            agent_kind="main",
        )
        assert Path(log_path).name == "agent_main_abc.log"
        assert Path(summary_path).name == "summary_trace_main_abc.jsonl"

    def test_empty_trace_keeps_current_names(self, tmp_path):
        log_path, summary_path = build_run_log_paths(
            config_dict={"logs_dump_dir": str(tmp_path), "trace_id": "  "},
            run_id="abc",
            agent_kind="main",
        )
        assert Path(log_path).name == "agent_main_abc.log"
        assert Path(summary_path).name == "summary_trace_main_abc.jsonl"

    def test_trace_is_prepended_to_run_id_in_both_names(self, tmp_path):
        log_path, summary_path = build_run_log_paths(
            config_dict={"logs_dump_dir": str(tmp_path), "trace_id": "wf-42"},
            run_id="abc",
            agent_kind="main",
        )
        assert Path(log_path).name == "agent_main_wf-42_abc.log"
        assert Path(summary_path).name == "summary_trace_main_wf-42_abc.jsonl"

    def test_unsafe_trace_cannot_escape_logs_dir(self, tmp_path):
        log_path, _ = build_run_log_paths(
            config_dict={
                "logs_dump_dir": str(tmp_path),
                "trace_id": "../../etc/x",
            },
            run_id="abc",
            agent_kind="main",
        )
        assert Path(log_path).parent == tmp_path
        assert Path(log_path).name == "agent_main_______etc_x_abc.log"


class TestJsonlRecords:
    def test_absent_trace_emits_no_key(self, tmp_path):
        logger, log_path, _ = create_run_logger(
            config_dict={
                "logs_dump_dir": str(tmp_path),
                "save_logs": True,
                "logging_format": "jsonl",
                "console_trace": False,
            },
            run_id="abc",
            agent_kind="main",
        )
        logger.info("hello", extra={"event": "unit_test"})
        _close(logger)
        record = json.loads(
            Path(log_path).read_text(encoding="utf-8").splitlines()[0]
        )
        assert "trace_id" not in record

    def test_trace_is_stamped_on_every_record(self, tmp_path):
        logger, log_path, _ = create_run_logger(
            config_dict={
                "logs_dump_dir": str(tmp_path),
                "save_logs": True,
                "logging_format": "jsonl",
                "console_trace": False,
                "trace_id": "wf-42",
            },
            run_id="abc",
            agent_kind="main",
        )
        logger.info("one", extra={"event": "a"})
        logger.info("two", extra={"event": "b"})
        _close(logger)
        lines = Path(log_path).read_text(encoding="utf-8").splitlines()
        assert [json.loads(x)["trace_id"] for x in lines] == ["wf-42", "wf-42"]

    def test_raw_value_not_slug_lands_in_record(self, tmp_path):
        logger, log_path, _ = create_run_logger(
            config_dict={
                "logs_dump_dir": str(tmp_path),
                "save_logs": True,
                "logging_format": "jsonl",
                "console_trace": False,
                "trace_id": "wf/123",
            },
            run_id="abc",
            agent_kind="main",
        )
        logger.info("x", extra={"event": "a"})
        _close(logger)
        record = json.loads(
            Path(log_path).read_text(encoding="utf-8").splitlines()[0]
        )
        assert record["trace_id"] == "wf/123"

    def test_per_call_extra_wins(self, tmp_path):
        logger, log_path, _ = create_run_logger(
            config_dict={
                "logs_dump_dir": str(tmp_path),
                "save_logs": True,
                "logging_format": "jsonl",
                "console_trace": False,
                "trace_id": "wf-42",
            },
            run_id="abc",
            agent_kind="main",
        )
        logger.info("x", extra={"event": "a", "trace_id": "explicit"})
        _close(logger)
        record = json.loads(
            Path(log_path).read_text(encoding="utf-8").splitlines()[0]
        )
        assert record["trace_id"] == "explicit"


class TestTextRecords:
    def test_absent_trace_keeps_plain_format(self, tmp_path):
        logger, log_path, _ = create_run_logger(
            config_dict={"logs_dump_dir": str(tmp_path), "save_logs": True},
            run_id="abc",
            agent_kind="main",
        )
        logger.info("hello")
        _close(logger)
        line = Path(log_path).read_text(encoding="utf-8").splitlines()[0]
        assert "trace=" not in line
        assert line.endswith("hello")

    def test_trace_appears_in_text_line(self, tmp_path):
        logger, log_path, _ = create_run_logger(
            config_dict={
                "logs_dump_dir": str(tmp_path),
                "save_logs": True,
                "trace_id": "wf-42",
            },
            run_id="abc",
            agent_kind="main",
        )
        logger.info("hello")
        _close(logger)
        line = Path(log_path).read_text(encoding="utf-8").splitlines()[0]
        assert "[trace=wf-42] hello" in line
