"""Тесты, подменяющие пакет тулов ядра, возвращают исходные модули.

Иначе модуль исполнителя в ``sys.modules`` — новая копия, а код,
импортированный при сборке тестов (поверхности сервера), держит прежнюю:
подмена ``gc_core.agent_executor.X`` бьёт мимо, и тест молча проверяет не
то (так в полном прогоне прошёл целиком ход в тесте восстановления
вебхука, который должен был упасть на подменённой проверке состояния).
"""

import importlib
import sys
from contextlib import contextmanager

import gc_core
import gc_core.agent_executor  # снимок до подмены
import gc_core.run_summary_coordinator  # снимок до подмены
import pytest

_TRACKED = (
    "gc_core.agent_executor",
    "gc_core.run_summary_coordinator",
    "gc_core.tools.connect_mcp.stream_client",
)


def _current() -> dict:
    return {name: sys.modules.get(name) for name in _TRACKED}


def _assert_left_as_found(before: dict) -> None:
    assert _current() == before
    assert gc_core.agent_executor is before["gc_core.agent_executor"]
    assert (
        gc_core.run_summary_coordinator
        is before["gc_core.run_summary_coordinator"]
    )


def test_summary_triggers_leaves_core_modules_as_found():
    before = _current()
    module = importlib.import_module("test_summary_triggers")
    module.setup_module(module)
    module.teardown_module(module)
    _assert_left_as_found(before)


def test_backend_mode_switch_leaves_core_modules_as_found():
    before = _current()
    module = importlib.import_module("test_backend_mode_switch")
    module._stub_tools_modules()
    module._restore_tools_and_dependents()
    _assert_left_as_found(before)


@pytest.mark.parametrize(
    "module_name",
    [
        "test_processing_responses_backend_logging",
        "test_processing_responses_context_baseline",
        "test_processing_responses_feedback",
        "test_terminal_outcome",
    ],
)
def test_processing_stubs_restore_original_mcp_manager(module_name):
    before = _current()
    module = importlib.import_module(module_name)
    try:
        module.setup_module(module)
    finally:
        module.teardown_module(module)
    _assert_left_as_found(before)


def test_finish_gate_stub_restores_original_mcp_manager():
    before = _current()
    module = importlib.import_module("test_finish_gate_two_axis")
    with contextmanager(module._load_processing_responses.__wrapped__)():
        pass
    _assert_left_as_found(before)
