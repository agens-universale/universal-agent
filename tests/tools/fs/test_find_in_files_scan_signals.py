"""Честность сигналов скана в режиме каталога.

Одиночный файл при превышении размера отвечает ``file_too_large`` и прямо
говорит «не считай, что текста нет». Каталог тот же случай проглатывал.
Плюс: бюджет открытых файлов не должен срабатывать на полностью завершённом
скане, а размер файла не должен запрашиваться дважды.
"""

import json
from collections import Counter

from tools.fs.find_in_files import FindInFiles
from tests.helpers.tool_ctx import with_local_ctx


def _tool(tmp_path, extra=None):
    cfg = {"launch_root": str(tmp_path)}
    if extra:
        cfg["fs_tools"] = {"find_in_files": extra}
    return with_local_ctx(FindInFiles, config_dict=cfg)


def _call(tool, tmp_path, **kwargs):
    return json.loads(tool(path=str(tmp_path), **kwargs))


def _force_python(tool, monkeypatch):
    monkeypatch.setattr(type(tool._backend), "has_grep", False, raising=False)
    return tool


def test_dir_mode_reports_files_skipped_as_too_large(tmp_path, monkeypatch):
    """Python walk: oversized files are counted, not silently grepped."""
    (tmp_path / "big.log").write_text("NEEDLE " * 40, encoding="utf-8")
    (tmp_path / "ok.py").write_text("nothing here\n", encoding="utf-8")

    tool = _force_python(_tool(tmp_path, {"max_file_bytes": 50}), monkeypatch)
    data = _call(tool, tmp_path, pattern="NEEDLE", query_mode="text")

    assert data["ok"] is True
    assert data.get("search_backend") == "python"
    assert data["count"] == 0
    assert data["files_too_large"] == 1
    assert data["guidance"]


def test_single_file_mode_still_reports_file_too_large(tmp_path):
    """Ветка одиночного файла — эталон, её поведение не меняем."""
    big = tmp_path / "big.log"
    big.write_text("NEEDLE " * 40, encoding="utf-8")

    tool = _tool(tmp_path, {"max_file_bytes": 50})
    data = json.loads(tool(path=str(big), pattern="NEEDLE", query_mode="text"))

    assert data["stop_reason"] == "file_too_large"
    assert data["scan_complete"] is False


def test_no_oversize_files_keeps_the_payload_slim(tmp_path):
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")

    data = _call(_tool(tmp_path), tmp_path, pattern="NEEDLE", query_mode="text")

    assert data["count"] == 1
    assert "files_too_large" not in data


def test_open_budget_exactly_met_is_a_complete_scan(tmp_path):
    """Бюджет израсходован ровно — значит его хватило, а не «слишком много»."""
    (tmp_path / "a.py").write_text("foobar\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foobar\n", encoding="utf-8")

    tool = _tool(tmp_path, {"max_open_files_affix_or": 2})
    data = _call(tool, tmp_path, pattern="foo(bar|baz)", query_mode="python-re")

    assert data["count"] == 2
    assert data["scan_complete"] is True
    assert "stop_reason" not in data


def test_open_budget_exceeded_still_refuses(tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("foobar\n", encoding="utf-8")

    tool = _tool(tmp_path, {"max_open_files_affix_or": 2})
    data = _call(tool, tmp_path, pattern="foo(bar|baz)", query_mode="python-re")

    assert data["stop_reason"] == "too_many_files_opened"
    assert data["scan_complete"] is False


def test_show_excluded_dirs_is_answered_not_dropped(tmp_path, monkeypatch):
    """Флаг принимался в сигнатуре нативной ветки и не встречался в её теле.

    find режет каталоги сам, собрать список пропущенных там нечем — значит на
    такой вызов отвечает Python-ветка, а не молчит быстрая.
    """
    import utils.native_fs_search as native_fs_search

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("KEY = 1\n", encoding="utf-8")

    native_calls = []

    def spy(*args, **kwargs):
        native_calls.append(1)
        raise AssertionError("нативная ветка не должна отвечать на этот вызов")

    monkeypatch.setattr(native_fs_search, "run_native_grep", spy)

    tool = _tool(tmp_path)
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)

    data = _call(
        tool, tmp_path, pattern="KEY", query_mode="text", show_excluded_dirs=True
    )

    assert native_calls == []
    assert data["search_backend"] == "python"
    assert any(e["path"].endswith("node_modules") for e in data["excluded_dirs"])


def test_file_size_is_probed_once_per_file(tmp_path, monkeypatch):
    """Python walk: размер уже посчитан при обходе — второй запрос лишний round-trip."""
    (tmp_path / "a.py").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("needle there\n", encoding="utf-8")

    tool = _force_python(_tool(tmp_path), monkeypatch)
    backend_cls = type(tool._backend)
    original = backend_cls.getsize
    seen = []

    def counting(self, p):
        seen.append(p)
        return original(self, p)

    monkeypatch.setattr(backend_cls, "getsize", counting)
    data = _call(tool, tmp_path, pattern="needle", query_mode="text")

    assert data.get("search_backend") == "python"
    assert data["count"] == 2
    per_file = Counter(seen)
    assert per_file and all(n == 1 for n in per_file.values()), per_file


def test_config_default_limit_applies_when_caller_omits_limit(tmp_path):
    """Ключ обещает «limit, когда вызывающий его не передал» — и должен работать.

    До правки `limit` имел жёсткое умолчание 100, а `_dispatch` отвергал None,
    так что правая ветвь `limit if limit is not None else config.default_limit`
    была недостижима, и ключ конфига не делал ничего.
    """
    (tmp_path / "a.py").write_text("\n".join(["needle"] * 10) + "\n", encoding="utf-8")

    tool = _tool(tmp_path, {"default_limit": 3})
    data = _call(tool, tmp_path, pattern="needle", query_mode="text")

    assert data["count"] == 3
    assert data["stop_reason"] == "match_limit"


def test_explicit_limit_still_wins_over_config(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(["needle"] * 10) + "\n", encoding="utf-8")

    tool = _tool(tmp_path, {"default_limit": 3})
    data = _call(tool, tmp_path, pattern="needle", query_mode="text", limit=7)

    assert data["count"] == 7


def test_limit_is_still_validated_when_given(tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    tool = _tool(tmp_path)

    for bad in (0, -1, "5", True):
        data = _call(tool, tmp_path, pattern="needle", query_mode="text", limit=bad)
        assert data["ok"] is False, bad
        assert data["error"]["field"] == "limit", bad
