"""Ошибка обхода дерева — доменный конверт, а не «внутренний сбой тула».

Соседи по реестру (list_files, find_files_by_name) мапят PermissionError и
прочие исключения обхода в свой конверт. find_in_files не мапил ничего: всё
улетало в общий перехватчик apply_tool с советом «не повторяй тот же вызов»,
который прямо противоречит сообщению самой ошибки «сузьте путь».
"""

import json

import pytest

from backends.protocol import WalkOutputTooLargeError
from tests.harbor_support.fakes import FakeBackend, bind_os_backend
from tools.fs.find_in_files import FindInFiles

_FILES = {"/work/app.py": "needle here\n"}


class _RaisingWalkBackend(FakeBackend):
    def __init__(self, files, exc):
        super().__init__(files)
        self._exc = exc

    def iter_walk(self, path, *, should_descend=None):
        raise self._exc
        yield  # pragma: no cover - генератор


def _run(exc):
    tool = FindInFiles.__new__(FindInFiles)
    bind_os_backend(tool, _RaisingWalkBackend(_FILES, exc))
    return json.loads(tool(path="/work", pattern="needle", query_mode="text"))


def test_permission_error_maps_to_permission_denied():
    data = _run(PermissionError("[Errno 13] Permission denied: '/work/sub'"))

    assert data["ok"] is False
    assert data["error"]["type"] == "permission_denied"
    assert "Permission denied" in data["error"]["message"]


def test_walk_output_too_large_keeps_its_actionable_message():
    exc = WalkOutputTooLargeError("/work", ceiling_chars=50_000_000)
    data = _run(exc)

    assert data["ok"] is False
    assert data["error"]["type"] == "unexpected_error"
    assert "narrow the path" in data["error"]["message"]


def test_missing_root_maps_to_a_domain_envelope():
    data = _run(FileNotFoundError("/work"))

    assert data["ok"] is False
    assert data["error"]["type"] == "unexpected_error"
    assert data["error"]["message"] == "/work"


def test_worker_failure_maps_to_a_domain_envelope(monkeypatch):
    """Исключение из воркера пробрасывается наружу через `raise exc` в фан-ине."""
    tool = FindInFiles.__new__(FindInFiles)
    bind_os_backend(tool, FakeBackend(_FILES))

    def boom(*args, **kwargs):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(FindInFiles, "_open_decode_text", boom)

    data = json.loads(tool(path="/work", pattern="needle", query_mode="text"))

    assert data["ok"] is False
    assert data["error"]["type"] == "unexpected_error"
    assert "worker exploded" in data["error"]["message"]
