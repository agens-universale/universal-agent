"""Attempt-scoped preview sink; final activity results remain authoritative."""

import logging
import sqlite3

from temporalio import activity

from gc_ua_temporalio.storage.views import SessionViews


def _emit(views, owner, operation, attempt, sequence, kind, payload):
    try:
        views.emit(owner, operation, attempt, sequence, kind, payload)
    except sqlite3.Error as exc:
        logging.getLogger(__name__).warning(
            "Preview unavailable: %s", type(exc).__name__
        )


def commit_preview(runtime, owner, operation, attempt=1):
    # Sequence zero is reserved for an idempotent receipt acknowledgement.
    _emit(
        SessionViews(runtime.store),
        owner,
        operation,
        attempt,
        0,
        "committed",
        {},
    )


class PreviewSink:
    def __init__(self, runtime, owner, operation, *, attempt=None):
        self.views = SessionViews(runtime.store)
        self.owner, self.operation = owner, operation
        self.attempt = activity.info().attempt if attempt is None else attempt
        self.sequence = 0
        self.bytes = 0
        self.emit("reset", {})

    def emit(self, kind, payload):
        self.sequence += 1
        _emit(
            self.views,
            self.owner,
            self.operation,
            self.attempt,
            self.sequence,
            kind,
            payload,
        )

    def delta(self, kind, text):
        # Preview is bounded independently of the authoritative model output.
        if self.bytes >= 64000 or self.sequence >= 500:
            return
        text = str(text)[:4000]
        self.bytes += len(text.encode())
        self.emit("preview", {"kind": kind, "text": text})

    async def mcp_progress(self, progress, total=None, message=None):
        self.delta(
            "mcp_progress", f"MCP {progress}/{total}: {message or ''}\n"
        )
