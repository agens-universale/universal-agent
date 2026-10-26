"""Проводка гварда в нативную ветку: предикат уходит ВНУТРЬ arun_native_grep.

Если фильтровать то, что вернулось, фильтруется уже обрезанный по limit список:
запрещённые совпадения съедают бюджет, разрешённые молча теряются. Тест
платформонезависим — сам рецепт grep здесь не запускается.
"""

import json

from gc_core.tools.fs.find_in_files import FindInFiles
from gc_core.utils import native_fs_search as native_fs_search
from gc_core.utils.native_fs_search import NativeGrepResult
from gc_testkit import as_coro
from gc_testkit.helpers.tool_ctx import with_local_ctx


def _cfg(root):
    return {
        "launch_root": str(root),
        "security.read_guard": {
            "enabled": True,
            "mode": "deny",
            "denied_file_patterns": [".env"],
        },
    }


def test_native_branch_hands_the_predicate_to_run_native_grep(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text("KEY=leak\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("KEY = 1\n", encoding="utf-8")

    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return NativeGrepResult(
            matches=[], truncated=False, hit_capture_cap=False
        )

    monkeypatch.setattr(native_fs_search, "arun_native_grep", as_coro(spy))

    tool = with_local_ctx(FindInFiles, _cfg(tmp_path))
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)

    json.loads(tool(path=str(tmp_path), pattern="KEY", query_mode="text"))

    allow_path = seen.get("allow_path")
    assert allow_path is not None
    assert allow_path(str(tmp_path / ".env")) is False
    assert allow_path(str(tmp_path / "app.py")) is True


def test_capture_cap_below_limit_stays_native_with_output_capped(
    tmp_path, monkeypatch
):
    """Потолок вывода — не лимит совпадений: 'increase limit' не помогает."""
    (tmp_path / "app.py").write_text("KEY = 1\n", encoding="utf-8")

    def spy(*args, **kwargs):
        return NativeGrepResult(
            matches=[
                {
                    "file": str(tmp_path / "app.py"),
                    "line": 1,
                    "end_line": 1,
                    "line_content": "KEY = 1",
                    "context": "KEY = 1\n",
                }
            ],
            truncated=True,
            hit_capture_cap=True,
        )

    monkeypatch.setattr(native_fs_search, "arun_native_grep", as_coro(spy))

    tool = with_local_ctx(FindInFiles, _cfg(tmp_path))
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)

    data = json.loads(
        tool(path=str(tmp_path), pattern="KEY", query_mode="text", limit=50)
    )

    assert data["search_backend"] == "grep"
    assert data["stop_reason"] == "output_capped"
    assert data["scan_complete"] is False
