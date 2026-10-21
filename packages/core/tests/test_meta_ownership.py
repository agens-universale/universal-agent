"""meta.runtime helpers (ownership constants)."""

from gc_core.persistence.meta_ownership import (
    RUNTIME_FLAG_NAMES,
    resolve_runtime_flag_name,
)


def test_resolve_runtime_flag_name():
    assert resolve_runtime_flag_name("summarizing") == "summarizing"
    assert resolve_runtime_flag_name("finalizing") == "finalizing"
    assert (
        resolve_runtime_flag_name("finish_gate_checking")
        == "finish_gate_checking"
    )


def test_resolve_runtime_flag_rejects_unknown():
    try:
        resolve_runtime_flag_name("tui_summarizing")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
