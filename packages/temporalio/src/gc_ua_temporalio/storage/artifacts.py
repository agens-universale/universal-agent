"""Immutable, content-addressed files outside Temporal event history."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from gc_core.execution.contracts import ArtifactRef, ExecutionFailure
from gc_core.execution.output_store import ToolCallOutputRecord


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid artifact digest")
        return self.root / digest

    def put(self, data: bytes, media_type="application/json") -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".artifact-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                if target.read_bytes() != data:
                    raise ExecutionFailure("Artifact content mismatch")
            # Persist the directory entry as well as the file contents on POSIX.
            if os.name == "posix":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temp).unlink(missing_ok=True)
        return ArtifactRef(digest, len(data), media_type)

    def read(self, ref: ArtifactRef) -> bytes:
        data = self._path(ref.digest).read_bytes()
        if (
            len(data) != ref.size_bytes
            or hashlib.sha256(data).hexdigest() != ref.digest
        ):
            raise ExecutionFailure("Artifact integrity check failed")
        return data


class ArtifactOutputs:
    """Read adapter for the existing session_read_tool_output tool."""

    def __init__(self, artifacts: ArtifactStore, owner: str):
        self.artifacts = artifacts
        self.owner = owner

    def save(self, record: ToolCallOutputRecord) -> ArtifactRef:
        payload = {"owner": self.owner, "record": asdict(record)}
        return self.artifacts.put(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        )

    def get(self, output_ref: str):
        path = self.artifacts._path(output_ref)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if hashlib.sha256(data).hexdigest() != output_ref:
            raise ExecutionFailure("Artifact integrity check failed")
        payload = json.loads(data)
        if payload["owner"] != self.owner:
            raise ExecutionFailure("Artifact belongs to another session")
        record = ToolCallOutputRecord(**payload["record"])
        record.output_ref = output_ref
        return record
