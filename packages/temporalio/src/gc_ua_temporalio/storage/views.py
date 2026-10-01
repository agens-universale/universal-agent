"""Read projection pages and expendable previews; UI metadata has its own table."""

import json

from gc_ua_temporalio.storage.sqlite import encode


class SessionViews:
    def __init__(self, store):
        self.store = store

    def owner(self, system, thread):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT owner FROM threads WHERE system=? AND thread=?",
                (system, thread),
            ).fetchone()
            return row["owner"] if row else None

    def history(self, system, thread, *, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid history cursor/limit")
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT seq,payload FROM messages WHERE system=? AND thread=? AND seq>? ORDER BY seq LIMIT ?",
                (system, thread, after, limit),
            ).fetchall()
            ui = db.execute(
                "SELECT payload FROM ui_meta WHERE system=? AND thread=?",
                (system, thread),
            ).fetchone()
        return {
            "messages": [
                {"seq": row["seq"], "message": json.loads(row["payload"])}
                for row in rows
            ],
            "after": rows[-1]["seq"] if rows else after,
            "meta": self.store.load_meta(system, thread),
            "ui_meta": json.loads(ui[0]) if ui else {},
        }

    def events(self, system, thread, *, after=0, limit=200):
        if after < 0 or not 1 <= limit <= 400:
            raise ValueError("Invalid event cursor/limit")
        owner = self.owner(system, thread)
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM view_events WHERE owner=? AND cursor>? ORDER BY cursor LIMIT ?",
                (owner, after, limit),
            ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def emit(self, owner, operation, attempt, sequence, kind, payload):
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO view_events(owner,operation,attempt,sequence,kind,payload) VALUES (?,?,?,?,?,?)",
                (owner, operation, attempt, sequence, kind, encode(payload)),
            )

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
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT payload FROM ui_meta WHERE system=? AND thread=?",
                (system, thread),
            ).fetchone()
            value = {**(json.loads(old[0]) if old else {}), **patch}
            db.execute(
                "INSERT INTO ui_meta VALUES (?,?,?) ON CONFLICT(system,thread) DO UPDATE SET payload=excluded.payload",
                (system, thread, encode(value)),
            )
        return value
