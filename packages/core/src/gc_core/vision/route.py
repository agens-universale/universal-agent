"""Opaque identity of the effective chat/completions recipient."""

import hashlib
import json
from dataclasses import dataclass, field

from gc_core.utils.llm_chat_client import PreparedChatCompletion


@dataclass(frozen=True)
class VisionRouteKey:
    url: str
    model: str
    fingerprint: str = field(repr=False)


def vision_route_key(prepared: PreparedChatCompletion) -> VisionRouteKey:
    # Distinct names are unordered; duplicate values retain their wire order.
    # A gateway may use the last value to choose a recipient.
    headers = {}
    for name, value in prepared.headers.items():
        name = name.lower()
        if name not in prepared.volatile_headers:
            headers.setdefault(name, []).append(value)
    values = {
        "mode": "chat_completions",
        "headers": headers,
        "network": dict(prepared.network_kwargs),
        "payload": dict(prepared.payload_kwargs),
    }
    encoded = json.dumps(
        values, sort_keys=True, separators=(",", ":")
    ).encode()
    return VisionRouteKey(
        prepared.url,
        prepared.resolved.model,
        hashlib.sha256(encoded).hexdigest(),
    )
