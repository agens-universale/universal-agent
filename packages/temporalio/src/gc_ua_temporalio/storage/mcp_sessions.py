"""Host generation and lease records survive resource worker restarts."""

import json
import time

from gc_ua_temporalio.storage.sqlite import encode


class McpSessions:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS mcp_sessions (
                id TEXT PRIMARY KEY, owner TEXT, server TEXT, revision TEXT,
                workspace TEXT, generation TEXT, host TEXT, status TEXT,
                expires REAL, tools TEXT)""")

    def get(self, session_id):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM mcp_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def create(
        self,
        session_id,
        owner,
        server,
        revision,
        workspace,
        generation,
        host,
        ttl,
    ):
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO mcp_sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    owner,
                    server,
                    revision,
                    workspace,
                    generation,
                    host,
                    "STARTING",
                    time.time() + ttl,
                    "[]",
                ),
            )

    def status(self, session_id, status, tools=None):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE mcp_sessions SET status=? WHERE id=?",
                (status, session_id),
            )
            if tools is not None:
                db.execute(
                    "UPDATE mcp_sessions SET tools=? WHERE id=?",
                    (encode(tools), session_id),
                )

    def renew(self, session_id, ttl):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE mcp_sessions SET expires=? WHERE id=?",
                (time.time() + ttl, session_id),
            )

    @staticmethod
    def describe(row):
        return {
            "session_id": row["id"],
            "generation": row["generation"],
            "server": row["server"],
            "revision": row["revision"],
            "tools": json.loads(row["tools"]),
            "status": row["status"],
        }
