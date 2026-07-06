"""force_compact_once: один безусловный проход сжатия (без проверки порога)."""
import types

from run_summary_coordinator import RunSummaryCoordinator


def _coord(monkeypatch, calls, backend="chat_completions"):
    state = types.SimpleNamespace(backend_api_mode=backend)
    coord = RunSummaryCoordinator(
        ctx=types.SimpleNamespace(), state=state, thread_session=None,
        summary_trigger_chars=10_000, is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend", logger=None,
    )
    monkeypatch.setattr(
        "run_summary_coordinator.run_summary_compaction",
        lambda **kw: calls.append(kw),
    )
    return coord


def test_force_compacts_once_ignoring_threshold(monkeypatch):
    calls = []
    coord = _coord(monkeypatch, calls)
    coord.force_compact_once()
    assert len(calls) == 1
    assert calls[0]["trigger_reason"] == "manual"
    # порог НЕ проверяется — сжали несмотря на пустую/малую историю
    assert calls[0]["trigger_threshold_chars"] == 10_000


def test_force_noop_in_responses_mode(monkeypatch):
    calls = []
    coord = _coord(monkeypatch, calls, backend="responses")
    coord.force_compact_once()
    assert calls == []
