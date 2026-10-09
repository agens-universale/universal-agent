"""Каталог набора на sys.path — локальные helpers.* резолвятся без корня репо.

И сброс пометок «модель не видит изображений» между тестами (см. фикстуру)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_tests_dir_str = str(_TESTS_DIR)
if _tests_dir_str not in sys.path:
    sys.path.insert(0, _tests_dir_str)


@pytest.fixture(autouse=True)
def _reset_image_rejects_between_tests():
    """Пометки живут на процесс, как runtime-уведомления: без сброса пометка
    одного теста снимала бы картинки в другом, и набор зависел бы от порядка.

    Здесь, а не в плагине тест-кита рядом со сбросом уведомлений: фикстура едет
    вместе с кодом, который сбрасывает. Набор, запущенный тест-китом другого
    чекаута (ревью из соседнего worktree — `tests/test_testkit_rootdir_gate.py`),
    не останется без сброса, а чужой старый набор не упадёт на импорте модуля,
    которого в его ядре нет."""
    from gc_core.vision import registry
    from vision_fakes import clear_image_rejects

    clear_image_rejects()
    registry._PROBES = registry.VisionProbeRegistry()
    yield
    clear_image_rejects()
    registry._PROBES = registry.VisionProbeRegistry()
