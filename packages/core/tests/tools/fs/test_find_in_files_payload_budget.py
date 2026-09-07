"""Размер полезной нагрузки fs_find_in_files ограничен.

line_content режется давно; context собирался из целых строк без бюджета, и на
минифицированном файле одна обычная выдача раздувалась до сотен мегабайт.
"""

import json

from gc_core.tools.fs.find_in_files import FindInFiles
from gc_testkit.helpers.tool_ctx import with_local_ctx


def _call(tmp_path, **kwargs):
    tool = with_local_ctx(FindInFiles, config_dict={"launch_root": str(tmp_path)})
    return json.loads(tool(path=str(tmp_path), **kwargs))


def _python_call(tmp_path, monkeypatch, **kwargs):
    """Python canon: native grep is a separate contract (see native_live tests)."""
    tool = with_local_ctx(FindInFiles, config_dict={"launch_root": str(tmp_path)})
    monkeypatch.setattr(type(tool._backend), "has_grep", False, raising=False)
    data = json.loads(tool(path=str(tmp_path), **kwargs))
    assert data.get("search_backend") == "python"
    return data


def test_context_is_bounded_on_a_minified_line(tmp_path):
    """Файл в одну длинную строку: окно контекста не тащит её целиком."""
    (tmp_path / "bundle.js").write_text(("x" * 5000 + "needle") * 40, encoding="utf-8")

    data = _call(tmp_path, pattern="needle", query_mode="text", limit=20)

    assert data["ok"] is True
    assert data["count"] > 0
    for m in data["matches"]:
        assert len(m["context"]) <= FindInFiles.max_context_chars
        assert len(m["line_content"]) <= FindInFiles.max_line_content_chars + 64


def test_whole_response_stays_small_on_a_minified_line(tmp_path):
    """Ответ целиком не должен расти как число_совпадений × длина_строки."""
    (tmp_path / "bundle.js").write_text(("y" * 5000 + "needle") * 40, encoding="utf-8")

    tool = with_local_ctx(FindInFiles, config_dict={"launch_root": str(tmp_path)})
    raw = tool(path=str(tmp_path), pattern="needle", query_mode="text", limit=20)

    assert len(raw) < 200_000


def test_normal_context_window_unchanged(tmp_path, monkeypatch):
    """Обычный исходник: контекст по-прежнему отдаёт окно вокруг совпадения."""
    (tmp_path / "app.py").write_text(
        "\n".join(f"line_{i}" for i in range(1, 21)) + "\n", encoding="utf-8"
    )

    data = _python_call(tmp_path, monkeypatch, pattern="line_10", query_mode="text")

    ctx = data["matches"][0]["context"]
    assert "line_5" in ctx
    assert "line_10" in ctx
    assert "line_15" in ctx
    assert "context_truncated" not in ctx


def test_long_line_keeps_the_match_visible(tmp_path, monkeypatch):
    """Обрезка контекста не должна прятать саму найденную строку."""
    (tmp_path / "one.txt").write_text("a" * 3000 + "NEEDLE" + "b" * 3000, encoding="utf-8")

    data = _python_call(tmp_path, monkeypatch, pattern="NEEDLE", query_mode="text")

    assert data["count"] == 1
    assert "NEEDLE" in data["matches"][0]["line_content"]
