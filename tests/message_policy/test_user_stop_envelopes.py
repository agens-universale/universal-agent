"""User-stop envelopes A/B — отдельные от cycle-stop status."""
from __future__ import annotations

import json

from message_policy.user_stop import (
    cancelled_by_user_output,
    not_started_due_to_user_stop_output,
)


def test_envelope_a_cancelled_by_user():
    raw = cancelled_by_user_output(
        "subagent_worker",
        meta={"steps": 4, "finish_reason": "stopped_by_user", "child_run_id": "cr1"},
    )
    data = json.loads(raw)
    assert data["tool"] == "subagent_worker"
    assert data["ok"] is False
    assert data["status"] == "cancelled_by_user"
    assert data["error"]["code"] == "cancelled_by_user"
    assert data["error"]["retryable"] is True
    assert data["meta"]["finish_reason"] == "stopped_by_user"
    assert data["meta"]["child_run_id"] == "cr1"
    assert "synthetic" not in data or data.get("synthetic") is not True


def test_envelope_b_not_started():
    raw = not_started_due_to_user_stop_output("fs_read_file")
    data = json.loads(raw)
    assert data["tool"] == "fs_read_file"
    assert data["ok"] is False
    assert data["status"] == "not_started_due_to_user_stop"
    assert data["error"] is None
    assert data["synthetic"] is True
    assert "skipped_on_cycle_stop" not in raw


def test_envelope_a_without_meta_omits_meta_key():
    data = json.loads(cancelled_by_user_output("fs_read_file"))
    assert "meta" not in data
    assert data["status"] == "cancelled_by_user"


def test_envelope_a_message_customizable():
    data = json.loads(
        cancelled_by_user_output("x", message="custom stop msg")
    )
    assert data["message"] == "custom stop msg"
    assert data["error"]["message"] == "custom stop msg"
