"""Versioned mailbox, checkpoint and projection read contracts."""

from dataclasses import dataclass, field
from typing import Any

from gc_core.execution.contracts import ArtifactRef


@dataclass
class UserMessage:
    message_id: str
    text: str

    def validate(self):
        if (
            not self.message_id
            or len(self.message_id) > 128
            or not self.text.strip()
            or len(self.text) > 16000
        ):
            raise ValueError(
                "Message requires an ID <= 128 chars and text <= 16000 chars"
            )


@dataclass
class InboxRequest:
    owner: str
    message: UserMessage
    schema_version: int = 1


@dataclass
class MessageDisposition:
    owner: str
    ids: list[str]
    disposition: str
    turn_id: str
    answer: str | None = None
    schema_version: int = 1


@dataclass
class Checkpoint:
    owner: str
    operation_id: str
    data: dict[str, Any] = field(default_factory=dict)
    ref: ArtifactRef | None = None
    schema_version: int = 1


@dataclass
class SessionRead:
    system_id: str
    thread_id: str
    after_seq: int = 0
    limit: int = 100
