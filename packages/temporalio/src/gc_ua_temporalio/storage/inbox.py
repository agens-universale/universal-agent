"""Accept and finish a logical message once, across workflow generations."""

import hashlib
import json
from dataclasses import asdict

from gc_ua_temporalio.storage.sqlite import ProjectionConflict, encode


class InboxLedger:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                owner TEXT, id TEXT, digest TEXT, body TEXT,
                disposition TEXT, turn_id TEXT, answer TEXT,
                PRIMARY KEY(owner,id))""")

    def accept(self, data):
        message = data.message
        message.validate()
        body = encode(asdict(message))
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM inbox WHERE owner=? AND id=?",
                (data.owner, message.message_id),
            ).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    raise ProjectionConflict(
                        "Message ID reused with different content"
                    )
                return self._receipt(row)
            pending = db.execute(
                "SELECT count(*) FROM inbox WHERE owner=? AND disposition='ACCEPTED'",
                (data.owner,),
            ).fetchone()[0]
            if pending >= 64:
                raise ValueError("Mailbox is full")
            db.execute(
                "INSERT INTO inbox VALUES (?,?,?,?,?,?,?)",
                (
                    data.owner,
                    message.message_id,
                    digest,
                    body,
                    "ACCEPTED",
                    None,
                    None,
                ),
            )
        return {
            "message_id": message.message_id,
            "disposition": "ACCEPTED",
            "turn_id": None,
            "answer": None,
        }

    def finish(self, data):
        if data.disposition not in (
            "PROCESSED",
            "STOPPED",
            "DROPPED_BY_STOP",
            "FAILED",
        ):
            raise ValueError("Invalid message disposition")
        with self.store.transaction() as db:
            for message_id in data.ids:
                row = db.execute(
                    "SELECT * FROM inbox WHERE owner=? AND id=?",
                    (data.owner, message_id),
                ).fetchone()
                if row is None:
                    raise ProjectionConflict("Unknown accepted message")
                if row["disposition"] != "ACCEPTED":
                    if (row["disposition"], row["turn_id"], row["answer"]) != (
                        data.disposition,
                        data.turn_id,
                        data.answer,
                    ):
                        raise ProjectionConflict("Message disposition changed")
                    continue
                db.execute(
                    "UPDATE inbox SET disposition=?,turn_id=?,answer=? WHERE owner=? AND id=?",
                    (
                        data.disposition,
                        data.turn_id,
                        data.answer,
                        data.owner,
                        message_id,
                    ),
                )
        return {"updated": len(data.ids)}

    def pending(self, owner):
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT body FROM inbox WHERE owner=? AND disposition='ACCEPTED' ORDER BY rowid",
                (owner,),
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    @staticmethod
    def _receipt(row):
        return {
            "message_id": row["id"],
            "disposition": row["disposition"],
            "turn_id": row["turn_id"],
            "answer": row["answer"],
        }
