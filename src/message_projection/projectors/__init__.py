"""Реэкспорт projectors пакета."""

from message_projection.projectors.strip_tagged_blocks import (
    strip_tagged_blocks,
    strip_think_blocks,
)

__all__ = ["strip_tagged_blocks", "strip_think_blocks"]
