"""Честность сигналов скана в режиме каталога.

Одиночный файл при превышении размера отвечает ``file_too_large`` и прямо
говорит «не считай, что текста нет». Каталог тот же случай проглатывал.
Плюс: бюджет открытых файлов не должен срабатывать на полностью завершённом
скане, а размер файла не должен запрашиваться дважды.
"""

import json
from collections import Counter

import pytest
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_testkit import as_coro
from gc_testkit.helpers.tool_ctx import with_local_ctx
from gc_testkit.settings import make_settings


def _tool(tmp_path, extra=None):
    cfg = {"launch_root": str(tmp_path)}
    if extra:
        cfg["fs_tools"] = {"find_in_files": extra}
    return with_local_ctx(FindInFiles, cfg)


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

    data = _call(
        _tool(tmp_path), tmp_path, pattern="NEEDLE", query_mode="text"
    )

    assert data["count"] == 1
    assert "files_too_large" not in data


def test_open_budget_exactly_met_is_a_complete_scan(tmp_path):
    """Бюджет израсходован ровно — значит его хватило, а не «слишком много»."""
    (tmp_path / "a.py").write_text("foobar\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foobar\n", encoding="utf-8")

    tool = _tool(tmp_path, {"max_open_files_affix_or": 2})
    data = _call(
        tool, tmp_path, pattern="foo(bar|baz)", query_mode="python-re"
    )

    assert data["count"] == 2
    assert data["scan_complete"] is True
    assert "stop_reason" not in data


def test_open_budget_exceeded_still_refuses(tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("foobar\n", encoding="utf-8")

    tool = _tool(tmp_path, {"max_open_files_affix_or": 2})
    data = _call(
        tool, tmp_path, pattern="foo(bar|baz)", query_mode="python-re"
    )

    assert data["stop_reason"] == "too_many_files_opened"
    assert data["scan_complete"] is False


@pytest.mark.parametrize("workers_threshold", [1, 20_000])
@pytest.mark.parametrize(
    ("limit", "stop_reason"),
    [(20, "regex_match_budget"), (5, "match_limit")],
)
def test_regex_match_budget_does_not_stop_directory_scan(
    tmp_path, workers_threshold, limit, stop_reason
):
    dense = "x" * (FindInFiles.max_regex_matches_examined + 100)
    (tmp_path / "a.txt").write_text(f"{dense}\nx\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text(f"{dense}\nx\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("x\n", encoding="utf-8")

    tool = _tool(tmp_path, {"content_workers_threshold": workers_threshold})
    data = _call(
        tool, tmp_path, pattern=r"(?=x)", query_mode="python-re", limit=limit
    )

    assert data["search_backend"] == "python"
    # Parallel workers can finish files in a different order.
    assert sorted((m["file"], m["line"]) for m in data["matches"]) == [
        (str(tmp_path / name), 1)
        for name in ("a.txt", "a.txt", "b.txt", "b.txt", "c.txt")
    ]
    assert data["count"] == 5
    assert data["stop_reason"] == stop_reason
    # The global limit can stop the last dense file before its own budget.
    budget_counts = (1, 2) if stop_reason == "match_limit" else (2,)
    assert data["regex_match_budget_files"] in budget_counts
    assert data["scan_complete"] is False
    assert data["more_matches_may_exist"] is True
    assert "more specific pattern" in data["guidance"]


def test_regex_open_budget_takes_priority_over_a_capped_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(FindInFiles, "max_regex_matches_examined", 10)
    (tmp_path / "a.txt").write_text("foobar" * 20, encoding="utf-8")
    (tmp_path / "b.txt").write_text("foobaz\n", encoding="utf-8")

    tool = _tool(tmp_path, {"max_open_files_affix_or": 1})
    data = _call(
        tool, tmp_path, pattern="foo(bar|baz)", query_mode="python-re"
    )

    assert data["count"] == 1
    assert data["stop_reason"] == "too_many_files_opened"
    assert data["regex_match_budget_files"] == 1
    assert data["scan_complete"] is False
    assert data["more_matches_may_exist"] is True


def test_show_excluded_dirs_is_answered_not_dropped(tmp_path, monkeypatch):
    """Флаг принимался в сигнатуре нативной ветки и не встречался в её теле.

    find режет каталоги сам, собрать список пропущенных там нечем — значит на
    такой вызов отвечает Python-ветка, а не молчит быстрая.
    """
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("KEY = 1\n", encoding="utf-8")

    native_calls = []

    def spy(*args, **kwargs):
        native_calls.append(1)
        raise AssertionError("нативная ветка не должна отвечать на этот вызов")

    monkeypatch.setattr(native_fs_search, "arun_native_grep", as_coro(spy))

    tool = _tool(tmp_path)
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)

    data = _call(
        tool,
        tmp_path,
        pattern="KEY",
        query_mode="text",
        show_excluded_dirs=True,
    )

    assert native_calls == []
    assert data["search_backend"] == "python"
    assert any(
        e["path"].endswith("node_modules") for e in data["excluded_dirs"]
    )


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
    (tmp_path / "a.py").write_text(
        "\n".join(["needle"] * 10) + "\n", encoding="utf-8"
    )

    tool = _tool(tmp_path, {"default_limit": 3})
    data = _call(tool, tmp_path, pattern="needle", query_mode="text")

    assert data["count"] == 3
    assert data["stop_reason"] == "match_limit"


def test_explicit_limit_still_wins_over_config(tmp_path):
    (tmp_path / "a.py").write_text(
        "\n".join(["needle"] * 10) + "\n", encoding="utf-8"
    )

    tool = _tool(tmp_path, {"default_limit": 3})
    data = _call(tool, tmp_path, pattern="needle", query_mode="text", limit=7)

    assert data["count"] == 7


def test_limit_is_still_validated_when_given(tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    tool = _tool(tmp_path)

    for bad in (0, -1, "5", True):
        data = _call(
            tool, tmp_path, pattern="needle", query_mode="text", limit=bad
        )
        assert data["ok"] is False, bad
        assert data["error"]["field"] == "limit", bad


def test_tool_description_uses_live_config_limits():
    cfg = {
        "fs_tools": {
            "find_in_files": {
                "default_limit": 7,
                "max_total_matches": 11,
            }
        }
    }
    spec = FindInFiles.tool_description(make_settings(cfg))
    prompt = FindInFiles.prompt_description(make_settings(cfg))
    limit = spec["parameters"]["properties"]["limit"]
    assert limit["maximum"] == 11
    assert "Default 7" in limit["description"]
    assert "maximum 11" in limit["description"]
    assert "default 7, max 11" in spec["description"]
    assert "limit (default 7, max 11" in prompt


def test_builtin_tools_data_schema_uses_live_config():
    from gc_core.tools.registry import get_builtin_tools_data

    tools = get_builtin_tools_data(
        make_settings(
            {
                "fs_tools": {
                    "find_in_files": {
                        "default_limit": 7,
                        "max_total_matches": 11,
                    }
                }
            }
        )
    )
    entry = tools["fs_find_in_files"]
    limit = entry["spec"]["parameters"]["properties"]["limit"]
    assert limit["maximum"] == 11
    assert "Default 7" in limit["description"]
    assert "limit (default 7, max 11" in entry["description"]


# --- честность движка: fallback_reason и output_capped -----------------------


def _native_ready(tool, monkeypatch, *, has_grep=True):
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", has_grep, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", has_grep, raising=False)


def _native_result(tmp_path, *, truncated=False, hit_cap=False, failure=None):
    from gc_core.utils.native_fs_search import NativeGrepResult

    return NativeGrepResult(
        matches=[
            {
                "file": str(tmp_path / "a.py"),
                "line": 1,
                "end_line": 1,
                "line_content": "KEY = 1",
                "context": "KEY = 1\n",
            }
        ],
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

    data = _call(
        tool, tmp_path, pattern="foo(bar|baz)", query_mode="python-re"
    )

    assert data["search_backend"] == "python"
    assert data["fallback_reason"] == "pattern_not_native"


def test_python_path_names_show_excluded_dirs(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(
        tool,
        tmp_path,
        pattern="KEY",
        query_mode="text",
        show_excluded_dirs=True,
    )

    assert data["fallback_reason"] == "show_excluded_dirs"


def test_single_file_names_its_python_path(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("KEY = 1\n", encoding="utf-8")

    data = json.loads(
        _tool(tmp_path)(path=str(f), pattern="KEY", query_mode="text")
    )

    assert data["search_backend"] == "python"
    assert data["fallback_reason"] == "single_file"


def test_native_answer_carries_no_fallback_reason(tmp_path, monkeypatch):
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(lambda *a, **k: _native_result(tmp_path)),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["search_backend"] == "grep"
    assert "fallback_reason" not in data


def test_native_path_does_not_count_candidates_before_grep(
    tmp_path, monkeypatch
):
    """find|wc -l is the Python-path cost guard; native goes straight to grep."""
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(lambda *a, **k: _native_result(tmp_path)),
    )

    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["search_backend"] == "grep"
    assert "fallback_reason" not in data


def test_capture_cap_below_limit_is_reported_as_output_capped(
    tmp_path, monkeypatch
):
    """Потолок вывода grep — не лимит совпадений и не повод молча читать всё
    заново с хоста: честный стоп с советом сузить запрос."""
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(
            lambda *a, **k: _native_result(
                tmp_path, truncated=True, hit_cap=True
            )
        ),
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


def test_capture_cap_with_native_failure_does_not_fall_to_python(
    tmp_path, monkeypatch
):
    """Непарсабельный хвост при cap: grep-путь, не download каждого файла."""
    from gc_core.utils import native_fs_search as native_fs_search
    from gc_core.utils.native_fs_search import NativeGrepResult

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(
            lambda *a, **k: NativeGrepResult(
                matches=[],
                truncated=True,
                hit_capture_cap=True,
                failure="native_error",
            )
        ),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", limit=50)

    assert data["search_backend"] == "grep"
    assert data["count"] == 0
    assert data["stop_reason"] == "output_capped"
    assert data["scan_complete"] is False
    assert "fallback_reason" not in data


def test_capture_capped_tail_preserves_native_hits(tmp_path, monkeypatch):
    from gc_core.utils import native_fs_search
    from gc_core.utils.native_fs_search import NativeGrepResult

    path = tmp_path / "big.log"
    path.write_text("KEY\n" + "x" * 1000 + "KEY\n", encoding="utf-8")
    rows = [
        {"file": str(path), "line": 1, "end_line": 1, "line_content": "KEY"},
        {
            "file": str(path),
            "line": 2,
            "end_line": 2,
            "line_content": "x" * 600,
            "line_content_truncated": True,
        },
    ]
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(lambda *a, **k: NativeGrepResult(rows, True, True)),
    )
    tool = _tool(tmp_path, {"max_file_bytes": 50})
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["search_backend"] == "grep"
    assert data["count"] == 2
    assert data["matches"][0]["line_content"] == "KEY"
    assert data["matches"][1]["line_content_truncated"] is True
    assert data["stop_reason"] == "output_capped"
    assert data["scan_complete"] is False
    assert "fallback_reason" not in data


@pytest.mark.parametrize(
    "total,limit,reason", [(5, 6, None), (31, 5, "match_limit")]
)
def test_unavailable_offsets_do_not_change_search_completeness(
    tmp_path, monkeypatch, total, limit, reason
):
    from gc_core.utils import native_fs_search
    from gc_core.utils.native_fs_search import NativeGrepResult

    line = "x" * 700 + "key"
    path = tmp_path / "big.log"
    path.write_text((line + "\n") * total, encoding="utf-8")
    rows = [
        {
            "file": str(path),
            "line": i + 1,
            "end_line": i + 1,
            "line_content": line,
        }
        for i in range(5)
    ]
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(
            lambda *a, **k: NativeGrepResult(
                rows, False, False, total_matches=total
            )
        ),
    )
    probes = []

    async def unavailable(_backend, lines, _plan):
        probes.append(lines)
        return [None] * len(lines)

    monkeypatch.setattr(
        native_fs_search, "alocate_native_grep_matches", unavailable
    )
    monkeypatch.setattr(FindInFiles, "max_native_match_locators", 2)
    tool = _tool(tmp_path, {"max_file_bytes": 50})
    _native_ready(tool, monkeypatch)

    data = _call(
        tool,
        tmp_path,
        pattern="KEY|OTHER",
        query_mode="python-re",
        case_insensitive=True,
        limit=limit,
    )

    assert data["search_backend"] == "grep"
    assert probes == [[line, line]]
    assert data["count"] == 5
    assert data.get("stop_reason") == reason
    assert data["scan_complete"] is (total == 5)
    assert data["more_matches_may_exist"] is (total > 5)
    assert all(
        "match location unavailable" in row["line_content"]
        for row in data["matches"]
    )
    if reason is not None:
        assert "Increase limit" in data["guidance"]
    assert "fallback_reason" not in data


# --- call-time exclude_dirs stay on the native path ----------------------------


def test_call_time_exclude_dirs_stay_on_the_native_path(tmp_path, monkeypatch):
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "x.py").write_text("KEY\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    seen_run = {}

    def run_spy(*args, **kwargs):
        seen_run.update(kwargs)
        return _native_result(tmp_path)

    monkeypatch.setattr(native_fs_search, "arun_native_grep", as_coro(run_spy))
    tool = _tool(tmp_path)
    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)

    vendor = str(tmp_path / "vendor")
    data = _call(
        tool, tmp_path, pattern="KEY", query_mode="text", exclude_dirs=[vendor]
    )

    assert data["search_backend"] == "grep"
    assert "fallback_reason" not in data
    assert seen_run["exclude_paths"] == [vendor]


# --- needle prefilter drives the Python path -----------------------------------


def _prefilter_ready(tool, monkeypatch, paths, failure=None):
    from gc_core.utils import native_fs_search as native_fs_search

    backend_cls = type(tool._backend)
    monkeypatch.setattr(backend_cls, "has_grep", True, raising=False)
    monkeypatch.setattr(backend_cls, "has_find", True, raising=False)
    calls = []

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return paths, failure

    monkeypatch.setattr(
        native_fs_search, "anative_prefilter_candidates", as_coro(spy)
    )
    return calls


def _track_opens(tool, monkeypatch):
    opened = []
    # `tool` обёрнут тест-адаптером: класс берём у настоящего тула.
    original = FindInFiles._open_decode_text

    def counting(self, abs_path, file_size=None):
        opened.append(abs_path)
        return original(self, abs_path, file_size)

    monkeypatch.setattr(FindInFiles, "_open_decode_text", counting)
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

    data = _call(
        tool,
        tmp_path,
        pattern="KEY(1|2)",
        query_mode="python-re",
        show_excluded_dirs=True,
    )

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


def test_prefiltered_candidates_still_honour_file_pattern(
    tmp_path, monkeypatch
):
    (tmp_path / "a.py").write_text("KEY1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("KEY2\n", encoding="utf-8")
    tool = _tool(tmp_path)
    calls = _prefilter_ready(
        tool, monkeypatch, [str(tmp_path / "a.py"), str(tmp_path / "b.txt")]
    )

    data = _call(
        tool,
        tmp_path,
        pattern="KEY(1|2)",
        query_mode="python-re",
        file_pattern="*.py",
    )

    assert data["prefilter"] == "grep"
    assert data["count"] == 1
    assert calls[0]["file_pattern"] == "*.py"


# --- обход с размерами и prune на стороне бэкенда ------------------------------


def test_walk_sizes_spare_the_per_file_stat():
    """Размер известен из обхода (find -printf %s) — getsize на каждый файл лишний."""
    from gc_testkit.harbor_fakes import FakeBackend, bind_os_backend

    class _Counting(FakeBackend):
        def __init__(self, files):
            super().__init__(files)
            self.getsize_calls = 0

        async def getsize(self, p):
            self.getsize_calls += 1
            return await super().getsize(p)

    backend = _Counting(
        {"/work/a.py": "needle here\n", "/work/b.py": "needle there\n"}
    )
    tool = FindInFiles.__new__(FindInFiles)
    tool = bind_os_backend(tool, backend)

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

    data = _call(
        tool,
        tmp_path,
        pattern="KEY",
        query_mode="text",
        show_excluded_dirs=True,
    )

    assert seen["prune_names"] == ()
    assert seen["prune_dot_dirs"] is False
    assert any(
        e["path"].endswith("node_modules") for e in data["excluded_dirs"]
    )


def test_missing_grep_is_named_before_a_non_native_pattern(
    tmp_path, monkeypatch
):
    """Без grep нативный путь невозможен в принципе — не предлагать агенту упрощать паттерн."""
    (tmp_path / "a.py").write_text("foo_suffix", encoding="utf-8")
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch, has_grep=False)

    data = _call(
        tool, tmp_path, pattern="(foo|bar)_suffix", query_mode="python-re"
    )

    assert data["search_backend"] == "python"
    assert data["fallback_reason"] == "no_grep"


async def test_prefiltered_entries_respect_the_path_boundary(tmp_path):
    root = tmp_path / "app"
    sibling = tmp_path / "app2" / "x.py"
    inside = root / "y.py"
    tool = _tool(tmp_path)

    rels = [
        e.rel
        async for e in tool._prefiltered_entries(
            str(root), [str(sibling), str(inside)]
        )
    ]

    assert rels == [
        "x.py",
        "y.py",
    ]  # сосед с общим префиксом имени — не «2/x.py»


def test_prefilter_timeout_is_reported_and_the_walk_answers(
    tmp_path, monkeypatch
):
    """Отказ префильтра не молчит: prefilter=timeout, ответ даёт обход с хоста."""
    (tmp_path / "a.py").write_text("foo_suffix", encoding="utf-8")
    tool = _tool(tmp_path)
    _prefilter_ready(tool, monkeypatch, None, failure="timeout")

    data = _call(
        tool, tmp_path, pattern="(foo|bar)_suffix", query_mode="python-re"
    )

    assert data["count"] == 1
    assert data["prefilter"] == "timeout"


# --- порядок совпадений при limit одинаков для обоих движков ------------------


def _native_disk_order(tmp_path, monkeypatch):
    """grep отдаёт совпадения в порядке find (диск), не по релевантности."""
    from gc_core.utils import native_fs_search as native_fs_search
    from gc_core.utils.native_fs_search import NativeGrepResult

    def _m(name, line):
        return {
            "file": str(tmp_path / name),
            "line": line,
            "end_line": line,
            "line_content": "needle",
            "context": "needle",
        }

    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return NativeGrepResult(
            matches=[
                _m("zzz.py", 1),
                _m("needle.py", 1),
                _m("needle.py", 2),
                _m("sub/a.py", 1),
            ],
            truncated=False,
            hit_capture_cap=False,
        )

    monkeypatch.setattr(native_fs_search, "arun_native_grep", as_coro(spy))
    return seen


def test_native_matches_are_ranked_like_the_python_path_before_the_limit(
    tmp_path, monkeypatch
):
    for name in ("zzz.py", "needle.py"):
        (tmp_path / name).write_text("needle", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("needle", encoding="utf-8")
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)
    seen = _native_disk_order(tmp_path, monkeypatch)

    data = _call(tool, tmp_path, pattern="needle", query_mode="text", limit=1)

    assert data["search_backend"] == "grep"
    assert data["count"] == 1
    assert data["matches"][0]["file"].endswith("needle.py")
    assert data["stop_reason"] == "match_limit"
    assert seen.get("max_matches") is None  # транспорт не режет до сортировки


def test_native_ranking_keeps_line_order_inside_a_file(tmp_path, monkeypatch):
    for name in ("zzz.py", "needle.py"):
        (tmp_path / name).write_text("needle", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("needle", encoding="utf-8")
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)
    _native_disk_order(tmp_path, monkeypatch)

    data = _call(tool, tmp_path, pattern="needle", query_mode="text", limit=3)

    files = [
        (m["file"].rsplit("/", 1)[-1].rsplit(chr(92), 1)[-1], m["line"])
        for m in data["matches"]
    ]
    assert files == [
        ("needle.py", 1),
        ("needle.py", 2),
        ("zzz.py", 1),
    ]  # имя, затем глубина
    assert data["stop_reason"] == "match_limit"


# --- честность флагов: files_too_large, пропуски, сбой native, слепое чтение с хоста ---


def test_files_too_large_makes_the_scan_incomplete(tmp_path, monkeypatch):
    """Кандидат не прочитан из-за размера — «0 совпадений» не доказательство отсутствия."""
    (tmp_path / "small.py").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / "dump.sql").write_text("x" * 200, encoding="utf-8")
    tool = _force_python(_tool(tmp_path, {"max_file_bytes": 50}), monkeypatch)

    data = _call(tool, tmp_path, pattern="NEEDLE", query_mode="text")

    assert data["count"] == 0
    assert data["files_too_large"] == 1
    assert data["scan_complete"] is False
    assert data["more_matches_may_exist"] is True
    assert (
        "stop_reason" not in data
    )  # причина не в лимите — флаги честные, stop_reason не выдумываем


def _skip_files(monkeypatch, *, binary=(), encoding=()):
    original = FindInFiles._open_decode_text

    async def fake_open(self, abs_path, *args, **kwargs):
        # Чтение файла у порта корутинное — подмена обязана быть такой же,
        # иначе разбор получит корутину вместо текста и молча ничего не найдёт.
        name = abs_path.replace("\\", "/").rsplit("/", 1)[-1]
        if name in binary:
            return None, 0, "binary"
        if name in encoding:
            return None, 0, "encoding"
        return await original(self, abs_path, *args, **kwargs)

    monkeypatch.setattr(FindInFiles, "_open_decode_text", fake_open)


def test_skipped_binary_and_encoding_are_reported(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"NEEDLE")
    (tmp_path / "weird.txt").write_bytes(b"NEEDLE")
    tool = _force_python(_tool(tmp_path), monkeypatch)
    _skip_files(monkeypatch, binary=("blob.bin",), encoding=("weird.txt",))

    data = _call(tool, tmp_path, pattern="NEEDLE", query_mode="text")

    assert data["count"] == 1
    assert data["skipped_binary"] == 1
    assert data["skipped_encoding"] == 1
    assert data["scan_complete"] is False  # недекодированный текст не проверен
    assert data["more_matches_may_exist"] is True


def test_binary_skips_alone_keep_the_scan_complete(tmp_path, monkeypatch):
    """Бинарник пропускает и native grep — иначе два движка разойдутся в честности."""
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"NEEDLE")
    tool = _force_python(_tool(tmp_path), monkeypatch)
    _skip_files(monkeypatch, binary=("blob.bin",))

    data = _call(tool, tmp_path, pattern="NEEDLE", query_mode="text")

    assert data["skipped_binary"] == 1
    assert "skipped_encoding" not in data
    assert data["scan_complete"] is True


def _no_host_reads(monkeypatch):
    def boom(self, *args, **kwargs):
        raise AssertionError("must not read file content from the host")

    monkeypatch.setattr(FindInFiles, "_open_decode_text", boom)


def test_native_timeout_is_an_honest_refusal_in_the_sandbox(
    tmp_path, monkeypatch
):
    """grep сорвался В ПЕСОЧНИЦЕ: отказ с именем сбоя, а не чтение каждого кандидата с хоста."""
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(
            lambda *a, **k: _native_result(tmp_path, failure="native_timeout")
        ),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)
    _sandbox(tool, monkeypatch)
    _no_host_reads(monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["ok"] is True
    assert data["search_backend"] == "grep"
    assert data["count"] == 0
    assert data["stop_reason"] == "native_timeout"
    assert (
        data["scan_complete"] is False
        and data["more_matches_may_exist"] is True
    )
    assert "fallback_reason" not in data
    assert "narrow" in data["guidance"].lower()


def test_native_failure_falls_back_to_python_on_a_local_backend(
    tmp_path, monkeypatch
):
    """Локальный бэкенд: чтение с хоста бесплатно — сбой grep обязан откатываться, а не отказывать.

    Гейт тот же, что у соседнего порога max_unfiltered_host_reads: отказ уместен
    только там, где чтение каждого кандидата действительно дорого.
    """
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(
            lambda *a, **k: _native_result(tmp_path, failure="native_error")
        ),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["ok"] is True
    assert data["search_backend"] == "python"  # откатились
    assert data["fallback_reason"] == "native_error"
    assert data["count"] == 1  # и ответили верно
    assert data["scan_complete"] is True


def _sandbox(tool, monkeypatch):
    monkeypatch.setattr(tool._backend, "is_sandbox", True, raising=False)


def test_sandbox_refuses_blind_host_reads_above_the_threshold(
    tmp_path, monkeypatch
):
    """COMPLEX без иглы на sandbox: сужения внутри sandbox нет, кандидатов больше порога — отказ до чтения."""
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("abc1\n", encoding="utf-8")
    tool = _tool(tmp_path, {"max_unfiltered_host_reads": 3})
    _native_ready(tool, monkeypatch)
    _sandbox(tool, monkeypatch)
    _no_host_reads(monkeypatch)

    data = _call(tool, tmp_path, pattern=r"[a-z]+\d", query_mode="python-re")

    assert data["ok"] is True and data["count"] == 0
    assert data["stop_reason"] == "unfiltered_host_reads"
    assert (
        data["scan_complete"] is False
        and data["more_matches_may_exist"] is True
    )
    assert data["candidate_count"] == 5 and data["candidate_limit"] == 3
    assert "narrow" in data["guidance"].lower()


def test_sandbox_refuses_when_the_prefilter_failed_on_a_big_tree(
    tmp_path, monkeypatch
):
    from gc_core.utils import native_fs_search as native_fs_search

    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("KEY_1 here\n", encoding="utf-8")
    tool = _tool(tmp_path, {"max_unfiltered_host_reads": 3})
    _native_ready(tool, monkeypatch)
    _sandbox(tool, monkeypatch)
    monkeypatch.setattr(
        native_fs_search, "prefilter_terms", lambda *a, **k: ["KEY_"]
    )
    monkeypatch.setattr(
        native_fs_search,
        "anative_prefilter_candidates",
        as_coro(lambda *a, **k: (None, "timeout")),
    )
    _no_host_reads(monkeypatch)

    data = _call(tool, tmp_path, pattern=r"KEY_\d+", query_mode="python-re")

    assert data["stop_reason"] == "unfiltered_host_reads"
    assert data["prefilter"] == "timeout"
    assert data["scan_complete"] is False


def test_blind_host_reads_below_the_threshold_proceed(tmp_path, monkeypatch):
    for i in range(2):
        (tmp_path / f"f{i}.py").write_text("abc1\n", encoding="utf-8")
    tool = _tool(tmp_path, {"max_unfiltered_host_reads": 3})
    _native_ready(tool, monkeypatch)
    _sandbox(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern=r"[a-z]+\d", query_mode="python-re")

    assert data["count"] == 2
    assert "stop_reason" not in data


def test_local_backend_never_refuses_blind_host_reads(tmp_path, monkeypatch):
    """Локально host и есть sandbox: чтение бесплатно, порог не действует."""
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("abc1\n", encoding="utf-8")
    tool = _tool(tmp_path, {"max_unfiltered_host_reads": 3})
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern=r"[a-z]+\d", query_mode="python-re")

    assert data["count"] == 5
    assert "stop_reason" not in data


def test_output_capped_on_native_names_the_densest_dirs(tmp_path, monkeypatch):
    """Обрезанный вывод grep: модель получает карту «где густо», а не голое «сузь»."""
    from gc_core.utils import native_fs_search as native_fs_search
    from gc_core.utils.native_fs_search import NativeGrepResult

    def _m(*parts):
        return {
            "file": str(tmp_path.joinpath(*parts)),
            "line": 1,
            "end_line": 1,
            "line_content": "KEY",
            "context": "KEY\n",
        }

    matches = [
        _m("src", "a.py"),
        _m("src", "a.py"),
        _m("src", "b.py"),
        _m("docs", "d.md"),
        _m("root.py"),
    ]
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(
            lambda *a, **k: NativeGrepResult(
                matches=list(matches), truncated=False, hit_capture_cap=True
            )
        ),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", limit=50)

    assert data["stop_reason"] == "output_capped"
    assert data["top_level_dirs_total"] == 2
    top = data["top_level_dirs"]
    assert top[0]["path"].replace("\\", "/").endswith("/src")
    assert (
        top[0]["match_files"] == 2
    )  # файлы с совпадениями, не сами совпадения
    assert top[1]["match_files"] == 1
    assert data["files_at_root"] == 1
    assert "densest" in data["guidance"]


def test_output_capped_map_is_only_for_the_capped_case(tmp_path, monkeypatch):
    """match_limit карту не несёт: модель и так получила совпадения для навигации."""
    from gc_core.utils import native_fs_search as native_fs_search

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        native_fs_search,
        "arun_native_grep",
        as_coro(lambda *a, **k: _native_result(tmp_path, truncated=True)),
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", limit=1)

    assert data["stop_reason"] == "match_limit"
    assert "top_level_dirs" not in data


def test_unfiltered_host_reads_guidance_names_the_real_reason(
    tmp_path, monkeypatch
):
    """Нет grep в песочнице: причина — нет grep, а не «у паттерна нет фиксированной подстроки»."""
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("KEY\n", encoding="utf-8")
    tool = _tool(tmp_path, {"max_unfiltered_host_reads": 3})
    _native_ready(tool, monkeypatch, has_grep=False)
    _sandbox(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text")

    assert data["stop_reason"] == "unfiltered_host_reads"
    guidance = data["guidance"]
    assert "no_grep" in guidance
    assert "fixed substring" not in guidance
    assert "breakdown" not in guidance and "top_level_dirs" in guidance


def test_unfiltered_host_reads_guidance_for_show_excluded_dirs(
    tmp_path, monkeypatch
):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("KEY\n", encoding="utf-8")
    tool = _tool(tmp_path, {"max_unfiltered_host_reads": 3})
    _native_ready(tool, monkeypatch)
    _sandbox(tool, monkeypatch)

    data = _call(
        tool,
        tmp_path,
        pattern="KEY",
        query_mode="text",
        show_excluded_dirs=True,
    )

    assert data["stop_reason"] == "unfiltered_host_reads"
    assert "show_excluded_dirs" in data["guidance"]
    assert "fixed substring" not in data["guidance"]


def test_native_branch_streams_top_k_and_maps_from_files_seen(
    tmp_path, monkeypatch
):
    """Тул просит у grep только K лучших, а карту «где густо» строит по всем файлам с хитами."""
    from gc_core.utils import native_fs_search as native_fs_search
    from gc_core.utils.native_fs_search import NativeGrepResult

    (tmp_path / "a.py").write_text("KEY = 1\n", encoding="utf-8")
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return NativeGrepResult(
            matches=[
                {
                    "file": str(tmp_path / "src" / "a.py"),
                    "line": 1,
                    "end_line": 1,
                    "line_content": "KEY",
                    "context": "KEY\n",
                }
            ],
            truncated=False,
            hit_capture_cap=True,
            total_matches=1000,
            files_seen={str(tmp_path / "src" / f"m{i}.py") for i in range(7)}
            | {str(tmp_path / "docs" / "d.md")},
        )

    monkeypatch.setattr(
        native_fs_search, "arun_native_grep", as_coro(fake_run)
    )
    tool = _tool(tmp_path)
    _native_ready(tool, monkeypatch)

    data = _call(tool, tmp_path, pattern="KEY", query_mode="text", limit=50)

    assert seen["top_k"] == 50 and callable(seen["rank_key"])
    assert data["stop_reason"] == "output_capped"
    assert (
        data["top_level_dirs"][0]["match_files"] == 7
    )  # по files_seen, не по одному показанному матчу
    assert data["top_level_dirs_total"] == 2


def test_unfiltered_host_reads_names_a_rejected_needle(tmp_path, monkeypatch):
    """Игла есть, но для sandbox-фильтра непригодна (не-ASCII при case_insensitive):
    совет «дай фиксированную подстроку» тут — дезориентация."""
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("Ключа\n", encoding="utf-8")
    tool = _tool(tmp_path, {"max_unfiltered_host_reads": 3})
    _native_ready(tool, monkeypatch)
    _sandbox(tool, monkeypatch)

    data = _call(
        tool,
        tmp_path,
        pattern="Ключ(а|б)",
        query_mode="python-re",
        case_insensitive=True,
    )

    assert data["stop_reason"] == "unfiltered_host_reads"
    guidance = data["guidance"]
    assert "cannot drive" in guidance
    assert "give the pattern a fixed substring" not in guidance
