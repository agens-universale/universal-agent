"""Shared exec-stream preview + store.put (spec D3/D4/D5/D7 clamp)."""

from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from config_modules.stored_outputs import get_tool_output_inline_max_chars
from tools.exec.truncate_output import truncate_head_tail
from tools.session.stored_outputs.tool_call_outputs_store import ToolCallOutputRecord

# Preview-only: transport capture cap drained the rest; total N is unknown.
# Must not be written into the store — the stored body is the captured prefix.
CAPTURE_CAP_MARKER = (
    "\n... [CUT — capture cap, rest discarded, total unknown]"
)


def clamp_max_output_chars(max_output_chars: int, config_dict: Optional[dict] = None) -> int:
    """D7: preview budget ≤ tool_output_inline_max_chars, чтобы envelope обычно не wrap-ился."""
    inline = get_tool_output_inline_max_chars(config_dict or {})
    if max_output_chars > inline:
        return inline
    return max_output_chars


def process_exec_stream(
    captured: str,
    max_output_chars: int,
    *,
    hit_capture_cap: bool,
    state: Any,
    call_id: Any,
    tool_name: str,
) -> dict:
    """Head+tail preview + optional store.put + output_ref.

    Возвращает dict: text, truncated, original_chars, kept_head_chars,
    kept_tail_chars, omitted_chars, hit_capture_cap, output_ref.
    """
    tr = truncate_head_tail(captured, max_output_chars)
    needs_ref = tr.truncated or hit_capture_cap
    output_ref = None
    if needs_ref and state is not None:
        store = getattr(state, "tool_call_outputs_store", None)
        if store is not None and not store.is_disabled():
            output_ref = str(uuid4())
            store.put(
                ToolCallOutputRecord(
                    output_ref=output_ref,
                    source_tool=tool_name,
                    call_id=call_id or "",
                    total_chars=len(captured),
                    text=captured,
                    truncated_in_message=tr.truncated,
                )
            )
    preview = tr.text
    if hit_capture_cap:
        preview = preview + CAPTURE_CAP_MARKER
    return {
        "text": preview,
        "truncated": tr.truncated,
        "original_chars": tr.original_chars,
        "kept_head_chars": tr.kept_head_chars,
        "kept_tail_chars": tr.kept_tail_chars,
        "omitted_chars": tr.omitted_chars,
        "hit_capture_cap": hit_capture_cap,
        "output_ref": output_ref,
    }


def apply_stream_meta_to_envelope(envelope: dict, prefix: str, meta: dict | None) -> None:
    """Добавить stream-meta в envelope только когда она нужна агенту.

    Короткий поток без hit_capture_cap — без полей (не шумим нулями/null).
    При preview-cut или hit_capture_cap — stats; ``*_output_ref`` только если не None.
    """
    if meta is None:
        return
    if not meta["truncated"] and not meta["hit_capture_cap"]:
        return
    envelope[f"{prefix}_original_chars"] = meta["original_chars"]
    envelope[f"{prefix}_kept_head_chars"] = meta["kept_head_chars"]
    envelope[f"{prefix}_kept_tail_chars"] = meta["kept_tail_chars"]
    envelope[f"{prefix}_omitted_chars"] = meta["omitted_chars"]
    envelope[f"{prefix}_hit_capture_cap"] = meta["hit_capture_cap"]
    if meta["output_ref"] is not None:
        envelope[f"{prefix}_output_ref"] = meta["output_ref"]
