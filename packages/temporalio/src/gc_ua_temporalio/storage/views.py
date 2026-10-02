"""Expendable preview events; final activity results remain authoritative."""

from gc_ua_temporalio.storage.sqlite import encode


class ViewEvents:
    def __init__(self, store):
        self.store = store

    def emit(self, owner, operation, attempt, sequence, kind, payload):
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO view_events(owner,operation,attempt,sequence,kind,payload) VALUES (?,?,?,?,?,?)",
                (owner, operation, attempt, sequence, kind, encode(payload)),
            )
