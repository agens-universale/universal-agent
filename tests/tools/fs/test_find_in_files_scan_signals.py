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


def test_file_size_is_never_probed_twice(tmp_path, monkeypatch):
    """Размер приезжает с обходом; если бэкенд его не знает — один getsize, не два."""
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
    assert all(n <= 1 for n in per_file.values()), per_file


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


# --- честность движка: fallback_reason и output_capped -----------------------

def _native_ready(tool, monkeypatch, *, has_grep=True):
    import utils.native_fs_search as native_fs_search

    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", has_grep, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", has_grep, raising=False)
    monkeypatch.setattr(native_fs_search, "count_native_candidates", lambda *a, **k: 1)


def _native_result(tmp_path, *, truncated=False, hit_cap=False, failure=None):
    from utils.native_fs_search import NativeGrepResult

    return NativeGrepResult(
        matches=[{
            "file": str(tmp_path / "a.py"), "line": 1, "end_line": 1,
            "line_content": "KEY = 1", "context": "KEY = 1\n",
        }],
        truncated=truncated,
        hit_capture_cap=hit_cap,
        failure=failure,
    )


def test_python_path_names_the_missing_grep(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch, has_grep=False)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["search_backend"] == "python"
    assert data["fallback_reason"] == "no_grep"


def test_python_path_names_a_non_native_pattern(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("foobar\n", encoding="utf-8")
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="foo(bar|baz)", query_mode="python-re")

    assert data["search_backend"] == "python"
    assert data["fallback_reason"] == "pattern_not_native"


def test_python_path_names_show_excluded_dirs(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", show_excluded_dirs=True)

    assert data["fallback_reason"] == "show_excluded_dirs"


def test_python_path_names_a_native_failure(tmp_path, monkeypatch):
    import utils.native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search, "run_native_grep",
        lambda *a, **k: _native_result(tmp_path, failure="native_timeout"),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["search_backend"] == "python"
    assert data["count"] == 1
    assert data["fallback_reason"] == "native_timeout"


def test_single_file_names_its_python_path(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("KEY = 1\n", encoding="utf-8")

    data = json.loads(_tool(tmp_path)(path=str(f), pattern="KEY", query_mode="text"))

    assert data["search_backend"] == "python"
    assert data["fallback_reason"] == "single_file"


def test_native_answer_carries_no_fallback_reason(tmp_path, monkeypatch):
    import utils.native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(native_fs_search, "run_native_grep", lambda *a, **k: _native_result(tmp_path))
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["search_backend"] == "grep"
    assert "fallback_reason" not in data


def test_capture_cap_below_limit_is_reported_as_output_capped(tmp_path, monkeypatch):
    """Потолок вывода grep — не лимит совпадений и не повод молча читать всё
    заново с хоста: честный стоп с советом сузить запрос."""
    import utils.native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search, "run_native_grep",
        lambda *a, **k: _native_result(tmp_path, truncated=True, hit_cap=True),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", limit=50)

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    assert data["stop_reason"] == "output_capped"
    assert data["scan_complete"] is False
    assert data["more_matches_may_exist"] is True
    assert "narrow" in data["guidance"].lower()
    assert "fallback_reason" not in data


# --- call-time exclude_dirs stay on the native path ----------------------------

def test_call_time_exclude_dirs_stay_on_the_native_path(tmp_path, monkeypatch):
    import utils.native_fs_search as native_fs_search

    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "x.py").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    seen_run, seen_count = {}, {}

    def run_spy(*args, **kwargs):
        seen_run.update(kwargs)
        return _native_result(tmp_path)

    def count_spy(*args, **kwargs):
        seen_count.update(kwargs)
        return 1

    monkeypatch.setattr(native_fs_search, "run_native_grep", run_spy)
    monkeypatch.setattr(native_fs_search, "count_native_candidates", count_spy)
    tool = _tool(tmp_path)
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)

    vendor = str(tmp_path / "vendor")
    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", exclude_dirs=[vendor])

    assert data["search_backend"] == "grep"
    assert "fallback_reason" not in data
    assert seen_run["exclude_paths"] == [vendor]
    assert seen_count["exclude_paths"] == [vendor]


# --- needle prefilter drives the Python path -----------------------------------

def _prefilter_ready(tool, monkeypatch, paths):
    import utils.native_fs_search as native_fs_search

    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)
    calls = []

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return paths

    monkeypatch.setattr(native_fs_search, "native_prefilter_candidates", spy)
    return calls


def _track_opens(tool, monkeypatch):
    opened = []
    original = type(tool)._open_decode_text

    def counting(self, abs_path, file_size=None):
        opened.append(abs_path)
        return original(self, abs_path, file_size)

    monkeypatch.setattr(type(tool), "_open_decode_text", counting)
    return opened


def test_python_path_opens_only_prefiltered_candidates(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("KEY2\n", encoding="utf-8")
    tool = _tool(tmp_path)
    calls = _prefilter_ready(tool, monkeypatch, [str(tmp_path / "a.py")])
    opened = _track_opens(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY(1|2)", query_mode="python-re")

    assert data["search_backend"] == "python"
    assert data["fallback_reason"] == "pattern_not_native"
    assert data["prefilter"] == "grep"
    assert data["count"] == 1
    assert opened == [str(tmp_path / "a.py")]
    assert calls[0]["terms"] == ["KEY"]


def test_prefilter_is_skipped_without_needles(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY1\n", encoding="utf-8")
    tool = _tool(tmp_path)
    calls = _prefilter_ready(tool, monkeypatch, [])

    data = _call(tool, tmp_path, pattern=r"KEY\d", query_mode="python-re")

    assert calls == []
    assert "prefilter" not in data
    assert data["count"] == 1


def test_prefilter_is_skipped_when_show_excluded_dirs(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY1\n", encoding="utf-8")
    tool = _tool(tmp_path)
    calls = _prefilter_ready(tool, monkeypatch, [])

    data = _call(tool, tmp_path, pattern="KEY(1|2)", query_mode="python-re", show_excluded_dirs=True)

    assert calls == []
    assert "prefilter" not in data
    assert data["count"] == 1


def test_prefilter_failure_falls_back_to_the_walk(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("KEY2\n", encoding="utf-8")
    tool = _tool(tmp_path)
    _prefilter_ready(tool, monkeypatch, None)

    data = _call(tool, tmp_path, pattern="KEY(1|2)", query_mode="python-re")

    assert "prefilter" not in data
    assert data["count"] == 2


def test_prefiltered_candidates_still_honour_file_pattern(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("KEY2\n", encoding="utf-8")
    tool = _tool(tmp_path)
    calls = _prefilter_ready(tool, monkeypatch, [str(tmp_path / "a.py"), str(tmp_path / "b.txt")])

    data = _call(tool, tmp_path, pattern="KEY(1|2)", query_mode="python-re", file_pattern="*.py")

    assert data["prefilter"] == "grep"
    assert data["count"] == 1
    assert calls[0]["file_pattern"] == "*.py"


# --- обход с размерами и prune на стороне бэкенда ------------------------------

def test_walk_sizes_spare_the_per_file_stat():
    """Размер известен из обхода (find -printf %s) — getsize на каждый файл лишний."""
    from tests.harbor_support.fakes import FakeBackend, bind_os_backend

    class _Counting(FakeBackend):
        def __init__(self, files):
            super().__init__(files)
            self.getsize_calls = 0

        def getsize(self, p):
            self.getsize_calls += 1
            return super().getsize(p)

    backend = _Counting({"/work/a.py": "needle here\n", "/work/b.py": "needle there\n"})
    tool = FindInFiles.__new__(FindInFiles)
    bind_os_backend(tool, backend)

    data = json.loads(tool(path="/work", pattern="needle", query_mode="text"))

    assert data["count"] == 2
    assert backend.getsize_calls == 0


def _spy_walk(tool, monkeypatch):
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", False, raising=False)
    seen = {}
    original = backend_cls.iter_walk_entries

    def spy(self, path, **kwargs):
        seen.update(kwargs)
        return original(self, path, **kwargs)

    monkeypatch.setattr(backend_cls, "iter_walk_entries", spy)
    return seen


def test_walk_prunes_hard_excludes_in_the_backend(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY\n", encoding="utf-8")
    tool = _tool(tmp_path)
    seen = _spy_walk(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["count"] == 1
    assert "node_modules" in seen["prune_names"]
    assert seen["prune_dot_dirs"] is True


def test_show_excluded_dirs_keeps_the_walk_unpruned(tmp_path, monkeypatch):
    """Список пропущенных каталогов собирает хост — бэкенд не должен их скрывать."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("KEY\n", encoding="utf-8")
    tool = _tool(tmp_path)
    seen = _spy_walk(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", show_excluded_dirs=True)

    assert seen["prune_names"] == ()
    assert seen["prune_dot_dirs"] is False
    assert any(e["path"].endswith("node_modules") for e in data["excluded_dirs"])
