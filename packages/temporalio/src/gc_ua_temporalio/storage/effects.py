"""Durable effect receipts and a workspace fence for unresolved operations."""

import json

from gc_ua_temporalio.storage.sqlite import ProjectionConflict, encode


class EffectLedger:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS effects (
                owner TEXT, operation TEXT, workspace TEXT, digest TEXT,
                status TEXT, plan TEXT, result TEXT,
                PRIMARY KEY(owner,operation))""")

    def get(self, owner, operation):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM effects WHERE owner=? AND operation=?",
                (owner, operation),
            ).fetchone()
            return dict(row) if row else None

    def begin(self, owner, operation, workspace, digest, plan):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM effects WHERE owner=? AND operation=?",
                (owner, operation),
            ).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ProjectionConflict(
                        "Effect operation ID reused with different input"
                    )
                return dict(row)
            blocked = db.execute(
                "SELECT operation FROM effects WHERE workspace=? AND status IN ('STARTED','UNKNOWN')",
                (workspace,),
            ).fetchone()
            if blocked:
                raise ProjectionConflict("Workspace has an unresolved effect")
            db.execute(
                "INSERT INTO effects VALUES (?,?,?,?,?,?,?)",
                (
                    owner,
                    operation,
                    workspace,
                    digest,
                    "STARTED",
                    encode(plan),
                    None,
                ),
            )
        return None

    def finish(self, owner, operation, status, result=None):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE effects SET status=?,result=? WHERE owner=? AND operation=?",
                (
                    status,
                    encode(result) if result is not None else None,
                    owner,
                    operation,
                ),
            )

    @staticmethod
    def result(row):
        return (
            json.loads(row["result"])
            if row.get("result") is not None
            else None
        )
