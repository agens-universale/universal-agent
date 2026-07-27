import json
from pathlib import Path
from types import SimpleNamespace

from summarize.summary_logging import append_summary_trace_record


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
