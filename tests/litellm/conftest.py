"""Common fixtures for tests/litellm/ — isolated env vars and temp paths."""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Empty env scoped to one test (no GIGACHAT_CREDENTIALS etc. leaking)."""
    for var in ("GIGACHAT_CREDENTIALS", "YANDEX_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield dict(os.environ)
