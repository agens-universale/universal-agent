"""Типы проекторов сообщений."""

from __future__ import annotations

from typing import Callable

Projector = Callable[[str], str]
