"""Immutable delivery state references; canonical agent history stays in ThreadStore."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckpointNamespace:
    workflow_id: str
    profile: str
    revision: str
    store_id: str
    system_id: str
    thread_id: str


@dataclass(frozen=True)
class NativeCheckpointRef:
    namespace: CheckpointNamespace
    digest: str
    byte_length: int
    chunk_count: int
    schema_version: int = 1


@dataclass(frozen=True)
class NativeCheckpointChunk:
    ref: NativeCheckpointRef
    index: int
    data: str = ""
