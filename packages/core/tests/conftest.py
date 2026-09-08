"""Каталог набора на sys.path — локальные helpers.* резолвятся без корня репо."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_tests_dir_str = str(_TESTS_DIR)
if _tests_dir_str not in sys.path:
    sys.path.insert(0, _tests_dir_str)
