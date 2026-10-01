"""Compatibility exports; the in-memory store is shared with execution state."""

from gc_core.execution.output_store import (
    ToolCallOutputRecord,
    ToolCallOutputsStore,
)

__all__ = ["ToolCallOutputRecord", "ToolCallOutputsStore"]
