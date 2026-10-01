"""Worker-only immutable delivery checkpoint storage in the technical ledger."""

import base64
import hashlib
import json
from dataclasses import asdict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from gc_ua_temporalio.native_checkpoint_contracts import (
    NativeCheckpointChunk,
    NativeCheckpointRef,
)

_SELECT_MANIFEST = "SELECT manifest FROM native_delivery_manifests WHERE namespace=? AND digest=?"


CHUNK_BYTES = 64 * 1024


class NativeCheckpointStore:
    def __init__(self, receipts):
        self.receipts = receipts
        with receipts.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS native_delivery_chunks (
                namespace TEXT, digest TEXT, idx INTEGER, data BLOB,
                PRIMARY KEY(namespace,digest,idx))""")
            db.execute("""CREATE TABLE IF NOT EXISTS native_delivery_manifests (
                namespace TEXT, digest TEXT, manifest TEXT,
                PRIMARY KEY(namespace,digest))""")

    @staticmethod
    def key(ref):
        if ref.schema_version != 1 or len(ref.digest) != 64:
            raise ValueError("Unsupported delivery checkpoint reference")
        if any(
            character not in "0123456789abcdef" for character in ref.digest
        ):
            raise ValueError("Invalid delivery checkpoint digest")
        if (
            ref.byte_length < 1
            or ref.chunk_count
            != (ref.byte_length + CHUNK_BYTES - 1) // CHUNK_BYTES
        ):
            raise ValueError("Invalid delivery checkpoint size")
        namespace = asdict(ref.namespace)
        if any(
            not isinstance(value, str) or not value
            for value in namespace.values()
        ):
            raise ValueError("Incomplete delivery checkpoint namespace")
        return json.dumps(namespace, sort_keys=True, separators=(",", ":"))

    def put(self, chunk):
        key = self.key(chunk.ref)
        if not 0 <= chunk.index < chunk.ref.chunk_count:
            raise ValueError("Invalid delivery checkpoint chunk index")
        raw = base64.b64decode(chunk.data, validate=True)
        expected = min(
            CHUNK_BYTES, chunk.ref.byte_length - chunk.index * CHUNK_BYTES
        )
        if len(raw) != expected:
            raise ValueError("Invalid delivery checkpoint chunk length")
        with self.receipts.transaction() as db:
            manifest = db.execute(
                _SELECT_MANIFEST,
                (key, chunk.ref.digest),
            ).fetchone()
            if manifest is not None and json.loads(
                manifest["manifest"]
            ) != asdict(chunk.ref):
                raise ValueError(
                    "Immutable delivery checkpoint manifest conflict"
                )
            old = db.execute(
                "SELECT data FROM native_delivery_chunks WHERE namespace=? AND digest=? AND idx=?",
                (key, chunk.ref.digest, chunk.index),
            ).fetchone()
            if old is not None and bytes(old["data"]) != raw:
                raise ValueError(
                    "Immutable delivery checkpoint chunk conflict"
                )
            db.execute(
                "INSERT OR IGNORE INTO native_delivery_chunks VALUES (?,?,?,?)",
                (key, chunk.ref.digest, chunk.index, raw),
            )
        return True

    def seal(self, ref):
        key = self.key(ref)
        encoded = json.dumps(
            asdict(ref), sort_keys=True, separators=(",", ":")
        )
        with self.receipts.transaction() as db:
            rows = db.execute(
                "SELECT idx,data FROM native_delivery_chunks WHERE namespace=? AND digest=? ORDER BY idx",
                (key, ref.digest),
            ).fetchall()
            raw = b"".join(bytes(row["data"]) for row in rows)
            if (
                [row["idx"] for row in rows] != list(range(ref.chunk_count))
                or len(raw) != ref.byte_length
                or hashlib.sha256(raw).hexdigest() != ref.digest
            ):
                raise ValueError("Incomplete or corrupt delivery checkpoint")
            previous = db.execute(
                _SELECT_MANIFEST,
                (key, ref.digest),
            ).fetchone()
            if previous is not None and previous["manifest"] != encoded:
                raise ValueError(
                    "Immutable delivery checkpoint manifest conflict"
                )
            db.execute(
                "INSERT OR IGNORE INTO native_delivery_manifests VALUES (?,?,?)",
                (key, ref.digest, encoded),
            )
        return ref

    def get(self, chunk):
        key = self.key(chunk.ref)
        if not 0 <= chunk.index < chunk.ref.chunk_count:
            raise ValueError("Invalid delivery checkpoint chunk index")
        with self.receipts.transaction() as db:
            manifest = db.execute(
                _SELECT_MANIFEST,
                (key, chunk.ref.digest),
            ).fetchone()
            if manifest is None or json.loads(manifest["manifest"]) != asdict(
                chunk.ref
            ):
                raise ValueError(
                    "Missing or mismatched delivery checkpoint manifest"
                )
            row = db.execute(
                "SELECT data FROM native_delivery_chunks WHERE namespace=? AND digest=? AND idx=?",
                (key, chunk.ref.digest, chunk.index),
            ).fetchone()
            if row is None:
                raise ValueError("Missing delivery checkpoint chunk")
            return NativeCheckpointChunk(
                chunk.ref,
                chunk.index,
                base64.b64encode(row["data"]).decode("ascii"),
            )


class NativeCheckpointActivities:
    def __init__(self, receipts):
        self.store = NativeCheckpointStore(receipts)

    @activity.defn(name="agent.delivery.put")
    async def put(self, chunk: NativeCheckpointChunk) -> bool:
        return self._invoke(self.store.put, chunk)

    @activity.defn(name="agent.delivery.seal")
    async def seal(self, ref: NativeCheckpointRef) -> NativeCheckpointRef:
        return self._invoke(self.store.seal, ref)

    @activity.defn(name="agent.delivery.get")
    async def get(self, chunk: NativeCheckpointChunk) -> NativeCheckpointChunk:
        return self._invoke(self.store.get, chunk)

    @staticmethod
    def _invoke(callback, value):
        try:
            return callback(value)
        except ValueError as exc:
            raise ApplicationError(
                str(exc), type="InvalidDeliveryCheckpoint", non_retryable=True
            ) from exc
