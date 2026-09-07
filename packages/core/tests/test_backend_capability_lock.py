"""Кэш возможностей бэкенда: повторный захват одного и того же замка.

``grep_exclude_dir`` / ``grep_include`` берут ``_cap_lock`` и внутри читают
``has_grep``, который берёт тот же нереентерабельный замок. В проде спасал
только порядок вызовов — холодное чтение зависает.
"""

import threading

from gc_core.backends import capabilities as capabilities
from gc_core.backends.local import LocalFsBackend


def _read_in_thread(backend, name, timeout=5.0):
    box = {}

    def run():
        box["value"] = getattr(backend, name)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive(), f"чтение {name} зависло: повторный захват _cap_lock"
    return box["value"]


def test_grep_include_cold_read_does_not_deadlock(monkeypatch):
    monkeypatch.setattr(capabilities, "probe_has_grep", lambda backend: False)
    backend = LocalFsBackend()

    assert _read_in_thread(backend, "grep_include") is False


def test_grep_exclude_dir_cold_read_does_not_deadlock(monkeypatch):
    monkeypatch.setattr(capabilities, "probe_has_grep", lambda backend: False)
    backend = LocalFsBackend()

    assert _read_in_thread(backend, "grep_exclude_dir") is False


def test_sub_capabilities_are_probed_when_grep_is_usable(monkeypatch):
    """Регрессия: при рабочем grep под-возможности всё ещё проверяются пробой."""
    monkeypatch.setattr(capabilities, "probe_has_grep", lambda backend: True)
    monkeypatch.setattr(capabilities, "probe_grep_include", lambda backend: True)
    monkeypatch.setattr(capabilities, "probe_grep_exclude_dir", lambda backend: True)
    backend = LocalFsBackend()

    assert _read_in_thread(backend, "grep_include") is True
    assert _read_in_thread(backend, "grep_exclude_dir") is True


def test_capability_values_are_cached_once(monkeypatch):
    calls = []

    def counting(backend):
        calls.append(1)
        return False

    monkeypatch.setattr(capabilities, "probe_has_grep", counting)
    backend = LocalFsBackend()

    _read_in_thread(backend, "has_grep")
    _read_in_thread(backend, "has_grep")

    assert len(calls) == 1
