"""Internal JSON Schema markers and their removal on the wire.

Our tool schemas carry bookkeeping keys (``x-optional-keys``, ``x-openai-strict``,
``x-preserve-empty-string``, ``x-suppress-optional-hint``): the local validator reads
them to decide which keys the model may omit. They are ours alone — the JSON Schema sent to a provider must stay
standard. Leaf module on purpose: the transport layer must be able to import it
without pulling in the tools package.
"""

from __future__ import annotations

from typing import Any

INTERNAL_MARKER_PREFIX = "x-"


def schema_without_internal_markers(schema: Any) -> Any:
    """Deep copy of ``schema`` with every ``x-*`` bookkeeping key removed.

    The source is left untouched: local validation still needs the markers after a
    request has been built.
    """
    if isinstance(schema, dict):
        return {
            k: schema_without_internal_markers(v)
            for k, v in schema.items()
            if not (isinstance(k, str) and k.startswith(INTERNAL_MARKER_PREFIX))
        }
    if isinstance(schema, list):
        return [schema_without_internal_markers(v) for v in schema]
    return schema
