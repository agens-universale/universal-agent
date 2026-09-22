"""Переполнение обхода (WalkOutputTooLargeError) — доменный конверт walk_too_large
во всех тулах, которые ходят по дереву через бэкенд, а не безликий unexpected_error."""

import json

from gc_core.backends.protocol import WalkOutputTooLargeError
from gc_core.tools.fs.find_files_by_name import FindFilesByName
from gc_core.tools.fs.list_files import ListFiles
from gc_testkit.harbor_fakes import FakeBackend, bind_os_backend

_FILES = {"/work/app.py": "print(1)\n"}


class _OverflowingBackend(FakeBackend):
    """Every traversal entry point overflows: iter_walk (list_files),
    iter_walk_entries (find_in_files / find_files_by_name), listdir (depth 0)."""

    async def iter_walk(self, path, *, should_descend=None):
        raise WalkOutputTooLargeError(path, ceiling_chars=50_000_000)
        yield  # pragma: no cover

    async def iter_walk_entries(self, path, *, should_descend=None, prune_names=(), prune_dot_dirs=False):
        raise WalkOutputTooLargeError(path, ceiling_chars=50_000_000)
        yield  # pragma: no cover

    async def listdir(self, path):
        raise WalkOutputTooLargeError(path, ceiling_chars=50_000_000)


def _tool(cls):
    tool = cls.__new__(cls)
    tool = bind_os_backend(tool, _OverflowingBackend(_FILES))
    return tool


def test_list_files_walk_reports_walk_too_large():
    data = json.loads(_tool(ListFiles)(path="/work", max_depth=2))
    assert data["ok"] is False
    assert data["error"]["type"] == "walk_too_large"
    assert "incomplete" in data["error"]["message"]
    assert "not proof" in data["error"]["message"]
    assert "narrow the path" in data["error"]["message"]
    assert "raise the ceiling" not in data["error"]["message"]


def test_list_files_listdir_reports_walk_too_large():
    data = json.loads(_tool(ListFiles)(path="/work"))
    assert data["ok"] is False
    assert data["error"]["type"] == "walk_too_large"
    assert "raise the ceiling" not in data["error"]["message"]


def test_find_files_by_name_reports_walk_too_large():
    data = json.loads(_tool(FindFilesByName)(path="/work", query="app"))
    assert data["ok"] is False
    assert data["error"]["type"] == "walk_too_large"
    assert "incomplete" in data["error"]["message"]
    assert "not proof" in data["error"]["message"]
    assert "narrow the path" in data["error"]["message"]
    assert "raise the ceiling" not in data["error"]["message"]
