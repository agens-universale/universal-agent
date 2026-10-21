"""Прежние правила runtime над meta develop — дословные копии кода до
перевода сессии на части v2 (``thread_store``, ``meta_ownership``,
``live_volume``). Оракул для ``gc_core.threads.runtime_rules``."""

from __future__ import annotations

from gc_core.attachments.numbering import label_numbers
from gc_core.persistence.live_volume import live_volume_from_messages
from gc_core.persistence.meta_ownership import resolve_runtime_flag_name
from gc_core.message_format.serialization import calc_history_chars_for_trigger


def runtime_section(meta):
    if not meta:
        return {}
    rt = meta.get("runtime") or {}
    return dict(rt) if isinstance(rt, dict) else {}


def read_image_seq(meta):
    value = runtime_section(meta).get("image_seq")
    return value if type(value) is int and value > 0 else 0


def bumped_image_seq(meta, messages):
    return max([read_image_seq(meta), *label_numbers(messages)])


def merge_runtime_into_meta(meta, patch) -> None:
    runtime = dict(meta.get("runtime") or {})
    runtime.update(dict(patch))
    meta["runtime"] = runtime


def apply_runtime_flag_to_meta(disk, flag, *, active, session_meta=None):
    del session_meta
    name = resolve_runtime_flag_name(flag)
    runtime = dict(disk.get("runtime") or {})
    if active:
        runtime[name] = True
    else:
        runtime.pop(name, None)
    if runtime:
        disk["runtime"] = runtime
    else:
        disk.pop("runtime", None)


def reset_run_flags_in_meta(
    disk, *, stale_summarizing=False, session_meta=None
):
    del session_meta
    runtime = dict(disk.get("runtime") or {})
    if stale_summarizing or runtime.get("summarizing"):
        runtime.pop("summarizing", None)
    runtime.pop("finalizing", None)
    runtime.pop("finish_gate_checking", None)
    if runtime:
        disk["runtime"] = runtime
    else:
        disk.pop("runtime", None)


def bump_live_volume_for_messages(
    meta, messages, *, tags=(), strip_think=True
):
    runtime = dict(meta.get("runtime") or {})
    try:
        chars = int(runtime.get("live_history_chars") or 0)
    except (TypeError, ValueError):
        chars = 0
    try:
        count = int(runtime.get("live_msg_count") or 0)
    except (TypeError, ValueError):
        count = 0
    delta = calc_history_chars_for_trigger(
        messages, tags=tags, strip_think=strip_think
    )
    runtime["live_history_chars"] = max(0, chars + delta)
    runtime["live_msg_count"] = max(0, count + len(messages))
    meta["runtime"] = runtime


def bump_image_seq(base_meta, fresh) -> None:
    image_seq = bumped_image_seq(base_meta, fresh)
    if image_seq > read_image_seq(base_meta):
        merge_runtime_into_meta(base_meta, {"image_seq": image_seq})


__all__ = [
    "apply_runtime_flag_to_meta",
    "bump_image_seq",
    "bump_live_volume_for_messages",
    "live_volume_from_messages",
    "merge_runtime_into_meta",
    "reset_run_flags_in_meta",
    "runtime_section",
]
