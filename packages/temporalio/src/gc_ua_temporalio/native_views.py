"""Native conversation pages from the actual core ThreadStore."""

import hashlib
import json
import uuid

from gc_core.thread_store_durable import ThreadStoreOperation

from gc_ua_temporalio.storage.views import SessionViews


def _prefix(messages):
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class NativeViews:
    def __init__(self, store, receipts, *, workflow_id=None):
        self.workflow_id = workflow_id
        self.store = store
        self.technical = SessionViews(receipts)

    def owner(self, system, thread):
        return self.store.read_snapshot(
            system, thread, include_state=False
        ).owner_id

    def history(self, system, thread, *, after=0, limit=100, token=None):
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid history cursor/limit")
        snapshot = self.store.read_snapshot(system, thread)
        messages = snapshot.state.messages
        reset = after > len(messages) or (
            token is not None and token != _prefix(messages[:after])
        )
        offset = 0 if reset else after
        end = min(len(messages), offset + limit)
        return {
            "messages": [
                {"seq": index + 1, "message": messages[index]}
                for index in range(offset, end)
            ],
            "after": end,
            "token": _prefix(messages[:end]),
            "generation": snapshot.generation,
            "reset": reset,
            "meta": snapshot.meta,
            "ui_meta": snapshot.meta.get("tui", {}),
        }

    def events(self, system, thread, *, after=0, limit=200):
        if after < 0 or not 1 <= limit <= 400:
            raise ValueError("Invalid event cursor/limit")
        owner = (
            self.workflow_id(system, thread)
            if self.workflow_id
            else self.owner(system, thread)
        )
        if not owner:
            return []
        # Native operations are scoped to owner/turn-N. No legacy projector read.
        with self.technical.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM view_events WHERE (owner=? OR substr(owner,1,?)=?) AND cursor>? ORDER BY cursor LIMIT ?",
                (owner, len(owner) + 1, owner + "/", after, limit),
            ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def patch_ui(self, system, thread, patch):
        if set(patch) - {"title", "pinned", "archived"}:
            raise ValueError("Only title, pinned and archived are editable")
        if "title" in patch and (
            not isinstance(patch["title"], str) or len(patch["title"]) > 200
        ):
            raise ValueError("Invalid title")
        if any(
            type(patch[key]) is not bool
            for key in ("pinned", "archived")
            if key in patch
        ):
            raise ValueError("UI flags must be boolean")
        snapshot = self.store.read_snapshot(
            system, thread, include_state=False
        )
        receipt = self.store.commit_operation(
            system,
            thread,
            ThreadStoreOperation(
                uuid.uuid4().hex,
                "update_tui",
                {"patch": patch},
                generation=snapshot.generation,
            ),
        )
        return receipt.meta.get("tui", {})
