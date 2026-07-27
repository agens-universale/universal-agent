"""Step 3 tests: exec tools — head+tail preview, store.put + refs, meta fields.

Спека: docs_md/actual/2026-07-24-exec-output-head-tail-truncate.md (D3, D4, D5, D7-updated).
D7 (updated): единый внешний envelope — NO skip-wrap по refs; exec-envelope с refs
врапится через generic maybe_store_and_wrap как любой длинный output.
"""
import json
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from config_modules.stored_outputs import DEFAULT_TOOL_OUTPUT_INLINE_MAX_CHARS
from tools.exec.execute_command import ExecuteCommand
from tools.exec.run_python_code import RunPythonCode
from tools.session.stored_outputs.tool_call_outputs_store import ToolCallOutputsStore
from tools.session.stored_outputs.wrapping import maybe_store_and_wrap_tool_output

from tests.helpers.tool_ctx import with_local_ctx


def _make_state():
    """State with a real ToolCallOutputsStore for store.put verification."""
    return SimpleNamespace(tool_call_outputs_store=ToolCallOutputsStore())


def _make_tool(cls, *, state=None, call_id="test-call-1", config_dict=None):
    return with_local_ctx(cls, state=state, call_id=call_id, config_dict=config_dict)


# ---------------------------------------------------------------------------
# ExecuteCommand: head+tail preview
# ---------------------------------------------------------------------------

class TestExecuteCommandPreview:
    def test_short_output_no_truncation_no_ref(self):
        """D5: короткий вывод — без ref, без store entry."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        r = json.loads(tool(command=f'"{sys.executable}" -c "print(123)"'))
        assert r["ok"] is True
        assert r["truncated"] is False
        assert r["stdout"].rstrip("\n") == "123"
        # Short stream: no meta noise, no ref
        assert "stdout_output_ref" not in r
        assert "stdout_original_chars" not in r
        assert "stdout_kept_head_chars" not in r
        assert "stdout_omitted_chars" not in r
        assert "stdout_hit_capture_cap" not in r
        assert "hint" not in r
        # Nothing in store
        assert len(state.tool_call_outputs_store) == 0

    def test_large_output_head_tail_preview(self):
        """D3: preview = head + tail + marker."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        # Generate ~500 chars
        r = json.loads(
            tool(
                command=f'"{sys.executable}" -c "print(\'A\'*500)"',
                max_output_chars=50,
            )
        )
        assert r["truncated"] is True
        assert r["stdout_original_chars"] == 501  # 500 A's + trailing newline from print
        # Бюджет max_output_chars уходит в head+tail; маркер сверху.
        assert r["stdout_kept_head_chars"] + r["stdout_kept_tail_chars"] == 50
        assert "[omitted" in r["stdout"]
        assert r["stdout"].startswith("AAAA")
        assert r["stdout"].endswith("AAAA") or r["stdout"].endswith("AAAA\n")
        assert r["stdout_kept_head_chars"] > 0
        assert r["stdout_kept_tail_chars"] > 0
        assert r["stdout_omitted_chars"] > 0
        assert "session_read_stored_tool_output" in r["hint"]
        assert "stdout_output_ref" in r["hint"]

    def test_short_output_no_page_hint(self):
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        r = json.loads(tool(command=f'"{sys.executable}" -c "print(1)"'))
        assert "hint" not in r

    def test_large_output_creates_store_ref(self):
        """D4: large output → tool puts record in store, envelope has output_ref."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        r = json.loads(
            tool(
                command=f'"{sys.executable}" -c "print(\'B\'*500)"',
                max_output_chars=50,
            )
        )
        # Ref present
        ref = r["stdout_output_ref"]
        assert ref is not None and len(ref) > 0
        # Record in store
        record = state.tool_call_outputs_store.get(ref)
        assert record is not None
        assert record.text == "B" * 500 + "\n"  # raw captured stdout (D4)
        assert record.total_chars == len(record.text)
        assert record.source_tool == "exec_command"

    def test_short_output_no_store_entry(self):
        """D5: short output → no store entry, read_stored would be not_found."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        r = json.loads(tool(command=f'"{sys.executable}" -c "print(42)"'))
        assert "stdout_output_ref" not in r
        assert len(state.tool_call_outputs_store) == 0

    def test_stderr_truncation_and_ref(self):
        """Stderr gets same head+tail + store treatment."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        # Write to stderr
        r = json.loads(
            tool(
                command=f'"{sys.executable}" -c "import sys; sys.stderr.write(\'E\'*500)"',
                max_output_chars=50,
            )
        )
        assert r["stderr_hit_capture_cap"] is False
        assert r["stderr_original_chars"] == 500
        assert r["stderr_omitted_chars"] > 0
        assert "[omitted" in r["stderr"]
        ref = r["stderr_output_ref"]
        assert ref is not None
        record = state.tool_call_outputs_store.get(ref)
        assert record is not None
        assert record.text == "E" * 500  # write() — no trailing newline

    def test_hit_capture_cap_creates_ref_even_if_preview_fits(self):
        """D4: if hit_capture_cap=True, ref is created even when preview fits."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        # Monkeypatch backend.exec to return hit_capture_cap=True with short output
        original_exec = tool._backend.exec

        def fake_exec(command, *, cwd=None, timeout=None, env=None):
            from backends.protocol import ExecResult
            return ExecResult(
                stdout="short",
                stderr="",
                return_code=0,
                timed_out=False,
                stdout_hit_capture_cap=True,
                stderr_hit_capture_cap=False,
            )

        tool._backend.exec = fake_exec
        try:
            r = json.loads(tool(command="echo test", max_output_chars=4000))
        finally:
            tool._backend.exec = original_exec

        # Output fits in preview but hit_capture_cap → ref needed for page
        assert r["stdout_hit_capture_cap"] is True
        assert r["stdout"] == "short"  # full preview
        assert r["truncated"] is False  # preview not cut
        ref = r["stdout_output_ref"]
        assert ref is not None
        record = state.tool_call_outputs_store.get(ref)
        assert record is not None
        assert record.text == "short"

    def test_meta_fields_balance(self):
        """original = kept_head + kept_tail + omitted."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        r = json.loads(
            tool(
                command=f'"{sys.executable}" -c "print(\'X\'*1000)"',
                max_output_chars=100,
            )
        )
        orig = r["stdout_original_chars"]
        head = r["stdout_kept_head_chars"]
        tail = r["stdout_kept_tail_chars"]
        omitted = r["stdout_omitted_chars"]
        assert head + tail + omitted == orig

    def test_no_state_no_ref_no_crash(self):
        """Tool must work without state (existing tests) — skip store, no ref."""
        tool = _make_tool(ExecuteCommand, state=None)
        r = json.loads(
            tool(
                command=f'"{sys.executable}" -c "print(\'Y\'*500)"',
                max_output_chars=50,
            )
        )
        assert r["truncated"] is True
        assert "[omitted" in r["stdout"]
        # Truncated stats present; no ref when state is None (omit null)
        assert r["stdout_omitted_chars"] > 0
        assert "stdout_output_ref" not in r

    def test_timeout_partial_output_gets_preview(self):
        """Timeout: partial stdout still gets preview (not discarded)."""
        state = _make_state()
        tool = _make_tool(ExecuteCommand, state=state)
        # Print before sleeping
        r = json.loads(
            tool(
                command=f'"{sys.executable}" -c "print(\'partial_out\'); import time; time.sleep(2)"',
                timeout_seconds=1,
                max_output_chars=4000,
            )
        )
        assert r["ok"] is False
        assert r["timed_out"] is True
        assert "partial_out" in r["stdout"]
        # Short partial fits preview → no stream meta
        assert "stdout_original_chars" not in r


class TestRunPythonCodePreview:
    def test_short_output_no_ref(self):
        state = _make_state()
        tool = _make_tool(RunPythonCode, state=state)
        r = json.loads(tool(code="print('hello')"))
        assert r["ok"] is True
        assert r["stdout"].rstrip("\n") == "hello"
        assert "stdout_output_ref" not in r
        assert "stdout_original_chars" not in r
        assert len(state.tool_call_outputs_store) == 0

    def test_large_output_head_tail_and_ref(self):
        state = _make_state()
        tool = _make_tool(RunPythonCode, state=state)
        r = json.loads(
            tool(code="print('Z' * 500)", max_output_chars=50)
        )
        assert r["truncated"] is True
        assert "[omitted" in r["stdout"]
        ref = r["stdout_output_ref"]
        assert ref is not None
        record = state.tool_call_outputs_store.get(ref)
        assert record is not None
        assert record.text == "Z" * 500 + "\n"

    def test_timeout_partial_output_not_discarded(self):
        """FIX: run_python_code currently discards stdout on timeout — must preview it."""
        state = _make_state()
        tool = _make_tool(RunPythonCode, state=state)
        r = json.loads(
            tool(
                code="print('before_sleep'); import time; time.sleep(2)",
                timeout_seconds=1,
                max_output_chars=4000,
            )
        )
        assert r["ok"] is False
        assert r["timed_out"] is True
        # partial stdout must be present; short → no stream meta
        assert "before_sleep" in r["stdout"]
        assert "stdout_original_chars" not in r

    def test_stderr_large_gets_ref(self):
        state = _make_state()
        tool = _make_tool(RunPythonCode, state=state)
        r = json.loads(
            tool(
                code="import sys; sys.stderr.write('E' * 500)",
                max_output_chars=50,
            )
        )
        assert r["stderr_omitted_chars"] > 0
        ref = r["stderr_output_ref"]
        assert ref is not None
        record = state.tool_call_outputs_store.get(ref)
        assert record is not None
        assert record.text == "E" * 500

    def test_no_state_no_crash(self):
        tool = _make_tool(RunPythonCode, state=None)
        r = json.loads(tool(code="print('W' * 500)", max_output_chars=50))
        assert r["truncated"] is True
        assert r["stdout_omitted_chars"] > 0
        assert "stdout_output_ref" not in r


# ---------------------------------------------------------------------------
# D7 (updated): единый envelope — exec-envelope с refs врапляется как любой тул
# ---------------------------------------------------------------------------

class TestExecEnvelopeUnifiedWrap:
    def test_envelope_with_ref_wraps_when_large(self):
        """D7-updated: exec-envelope с stdout_output_ref врапляется если > inline_limit."""
        state = _make_state()
        config = {"tool_output_inline_max_chars": 50}
        tool_res = json.dumps({
            "tool": "exec_command",
            "ok": True,
            "stdout": "preview..." + "x" * 200,
            "stdout_output_ref": "some-ref-123",
        })
        result = maybe_store_and_wrap_tool_output(
            tool_res, call_id="c1", tool_name="exec_command",
            state=state, config=config,
        )
        assert result != tool_res
        parsed = json.loads(result)
        assert parsed.get("type") == "stored_tool_output"
        assert parsed.get("output_ref") is not None
        # exec-envelope is inside content (truncated to inline_limit)
        assert "exec_command" in parsed["content"]
        assert parsed["truncated"] is True

    def test_envelope_with_ref_passes_through_if_short(self):
        """D7-updated: exec-envelope с ref, но короткий — проходит as-is (no wrap)."""
        state = _make_state()
        config = {"tool_output_inline_max_chars": 1000}
        tool_res = json.dumps({
            "tool": "exec_command",
            "ok": True,
            "stdout": "preview...",
            "stdout_output_ref": "some-ref-123",
        })
        result = maybe_store_and_wrap_tool_output(
            tool_res, call_id="c2", tool_name="exec_command",
            state=state, config=config,
        )
        assert result == tool_res

    def test_stderr_ref_envelope_wraps_when_large(self):
        """D7-updated: exec-envelope с stderr_output_ref врапляется если > inline_limit."""
        state = _make_state()
        config = {"tool_output_inline_max_chars": 50}
        tool_res = json.dumps({
            "tool": "exec_command",
            "ok": False,
            "stderr": "err preview" + "e" * 200,
            "stderr_output_ref": "err-ref-456",
        })
        result = maybe_store_and_wrap_tool_output(
            tool_res, call_id="c3", tool_name="exec_command",
            state=state, config=config,
        )
        assert result != tool_res
        parsed = json.loads(result)
        assert parsed.get("type") == "stored_tool_output"

    def test_non_json_tool_res_wraps_if_large(self):
        """Non-JSON large output → normal wrapping."""
        state = _make_state()
        config = {"tool_output_inline_max_chars": 50}
        tool_res = "x" * 200
        result = maybe_store_and_wrap_tool_output(
            tool_res, call_id="c4", tool_name="other_tool",
            state=state, config=config,
        )
        assert result != tool_res

    def test_read_stored_tool_output_still_skipped(self):
        """READ_STORED_TOOL_OUTPUT_NAME skip must still work (existing, not D7)."""
        state = _make_state()
        config = {"tool_output_inline_max_chars": 50}
        tool_res = json.dumps({"type": "stored_tool_output", "content": "x" * 200})
        from constants.tool_names import READ_STORED_TOOL_OUTPUT_NAME
        result = maybe_store_and_wrap_tool_output(
            tool_res, call_id="c5", tool_name=READ_STORED_TOOL_OUTPUT_NAME,
            state=state, config=config,
        )
        assert result == tool_res


class TestClampPreviewToInline:
    def test_max_output_chars_clamped_to_inline_limit(self):
        """D7: max_output_chars > inline → clamp, preview ≤ inline."""
        state = _make_state()
        tool = _make_tool(
            ExecuteCommand,
            state=state,
            config_dict={"tool_output_inline_max_chars": 80},
        )
        r = json.loads(
            tool(
                command=f'"{sys.executable}" -c "print(\'Q\'*500)"',
                max_output_chars=10_000,
            )
        )
        assert r["truncated"] is True
        # head+tail = clamped inline budget; маркер сверху не вжимаем.
        assert r["stdout_kept_head_chars"] + r["stdout_kept_tail_chars"] == 80
        assert "[omitted" in r["stdout"]
        assert r["stdout_output_ref"] is not None

    def test_run_python_clamps_the_same_way(self):
        state = _make_state()
        tool = _make_tool(
            RunPythonCode,
            state=state,
            config_dict={"tool_output_inline_max_chars": 60},
        )
        r = json.loads(tool(code="print('R' * 400)", max_output_chars=5000))
        assert r["truncated"] is True
        assert r["stdout_kept_head_chars"] + r["stdout_kept_tail_chars"] == 60
