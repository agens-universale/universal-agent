"""Pipeline проекторов текста для sink-профилей."""

from __future__ import annotations

from typing import Iterable, Sequence, Tuple

from message_projection.types import Projector


class MessageProjectionPipeline:
    """Последовательное применение projectors к plain text. State не пишет."""

    def __init__(self, projectors: Iterable[Projector]) -> None:
        self._projectors: Tuple[Projector, ...] = tuple(projectors)

    @property
    def projectors(self) -> Sequence[Projector]:
        return self._projectors

    def project(self, text: str) -> str:
        result = text if isinstance(text, str) else ""
        for projector in self._projectors:
            result = projector(result)
        return result
