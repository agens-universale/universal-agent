"""Conservative payload budget for workflow and activity wire values."""

import json
from dataclasses import asdict, is_dataclass

from gc_core.execution.contracts import ExecutionFailure

MAX_PAYLOAD_BYTES = 512_000


def validate_payload(value):
    data = asdict(value) if is_dataclass(value) else value
    if len(json.dumps(data).encode()) > MAX_PAYLOAD_BYTES:
        raise ExecutionFailure(
            "Execution payload exceeds the 512000-byte limit"
        )
