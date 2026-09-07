"""Physical-pixel coordinate helpers. All rects are in screen-physical pixels."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


def center_of(r: Rect) -> tuple[int, int]:
    return (r.x + r.width // 2, r.y + r.height // 2)


def point_in_rect(point: tuple[int, int], r: Rect) -> bool:
    x, y = point
    return r.x <= x < r.right and r.y <= y < r.bottom
