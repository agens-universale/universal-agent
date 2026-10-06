"""Native conversation pages from the actual core ThreadStore."""

import hashlib
import json
import uuid

from gc_core.thread_store_durable import ThreadStoreOperation


def _prefix(messages):
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class NativeViews:
    def __init__(self, store, *, workflow_id=None):
        self.workflow_id = workflow_id
        self.store = store

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
