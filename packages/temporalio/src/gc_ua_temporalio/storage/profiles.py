"""A profile revision is immutable within this deployment's durable storage."""

import hashlib

from gc_ua_temporalio.storage.sqlite import encode


class ProfilePins:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS profile_pins (kind TEXT,name TEXT,revision TEXT,digest TEXT,PRIMARY KEY(kind,name,revision))"
            )

    def pin(self, kind, name, profile):
        digest = hashlib.sha256(
            encode(profile.model_dump()).encode()
        ).hexdigest()
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO profile_pins VALUES (?,?,?,?)",
                (kind, name, profile.revision, digest),
            )
            saved = db.execute(
                "SELECT digest FROM profile_pins WHERE kind=? AND name=? AND revision=?",
                (kind, name, profile.revision),
            ).fetchone()[0]
            if saved != digest:
                raise ValueError(
                    f"Profile {kind}/{name}/{profile.revision} changed; use a new revision and keep the old worker for active runs"
                )
