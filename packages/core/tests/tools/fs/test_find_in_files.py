"""Integration tests for fs_find_in_files.

Covers scan-automaton: mode caps, refuse breakdown, exclude_dirs,
single-file / directory, A/B/C, workers, query_mode, prefilter,
slim RESPONSE-CONTRACT payload.
"""

import json
import os
from pathlib import Path

import pytest
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_testkit.helpers.tool_ctx import with_local_ctx

# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _call(tool, **kw):
    """Call FindInFiles and parse JSON response.

    Omitting query_mode exercises the tool default (python-re).
    Pass query_mode=... explicitly when a test needs text / dotall / invalid.
    """
    return json.loads(tool(**kw))


def _make_tree(base, files: dict):
    """Create files under base from {relative_path: content} dict."""
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")


def _assert_slim_success_keys(data):
    """Always-present keys on ok:true (RESPONSE-CONTRACT §3 / §12)."""
    for key in (
        "tool",
        "ok",
        "path",
        "pattern",
        "query_mode",
        "mode",
        "matches",
        "count",
        "scan_complete",
        "more_matches_may_exist",
        "case_insensitive",
    ):
        assert key in data
    assert data["ok"] is True
    assert data["tool"] == "fs_find_in_files"
    # never in agent payload
    for key in (
        "verbose",
        "debug_info",
        "search_stats",
        "search_scope",
        "needles",
        "content_workers",
        "pattern_mode",
        "case_insensitive_arg",
        "effective_ci",
        "stop_summary",
        "show_excluded_dirs",
    ):
        assert key not in data


# ------------------------------------------------------------------
# 1. Single file — basic
# ------------------------------------------------------------------

class TestSingleFile:
    def test_single_file_match(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("import os\nprint('hello')\nimport os\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="import")
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["mode"] == "file"
        assert data["scan_complete"] is True
        assert data["more_matches_may_exist"] is False
        assert "stop_reason" not in data
        assert "limit_eff" not in data
        assert "limit_requested" not in data
        assert "limit_capped" not in data
        assert all(m["file"].endswith("code.py") for m in data["matches"])
        assert [m["line"] for m in data["matches"]] == [1, 3]
        _assert_slim_success_keys(data)

    def test_single_file_no_match(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="zzz")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["matches"] == []
        assert data["scan_complete"] is True
        assert "stop_reason" not in data
        assert data["more_matches_may_exist"] is False

    def test_single_file_regex_multiline(self, tmp_path):
        f = tmp_path / "t.md"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="line1.*line3",
            query_mode="python-re-dotall",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["line"] == 1
        assert data["matches"][0]["end_line"] == 3

    def test_single_file_match_limit(self, tmp_path):
        f = tmp_path / "many.py"
        f.write_text("X\n" * 500, encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="X", limit=10)
        assert data["ok"] is True
        assert data["count"] == 10
        assert data["stop_reason"] == "match_limit"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data["limit_eff"] == 10
        # agent set limit=10 itself → no limit_requested
        assert "limit_requested" not in data
        assert "limit_capped" not in data
        assert "Increase limit" in data["guidance"]

    def test_single_file_limit_capped(self, tmp_path):
        f = tmp_path / "many.py"
        f.write_text("X\n" * 500, encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="X", limit=500)
        assert data["ok"] is True
        assert data["limit_eff"] == 300
        assert data["limit_requested"] == 500
        assert data["limit_capped"] is True
        assert data["count"] == 300
        assert data["stop_reason"] == "match_limit"
        assert "Increase limit" not in data["guidance"]
        assert "narrow path/pattern" in data["guidance"]

    def test_single_file_limit_capped_no_match(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="zzz", limit=500)
        assert data["ok"] is True
        assert data["limit_eff"] == 300
        assert data["limit_capped"] is True
        assert data["limit_requested"] == 500
        assert data["stop_reason"] == "requested_limit_capped"
        assert data["count"] == 0
        assert data["scan_complete"] is True
        assert "capped to 300" in data["guidance"]

    def test_single_file_case_insensitive(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("Hello\nHELLO\nhello\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="hello",
            case_insensitive=True,
        )
        assert data["ok"] is True
        assert data["count"] == 3
        assert data["case_insensitive"] is True

    def test_single_file_case_sensitive(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("Hello\nHELLO\nhello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="hello")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["case_insensitive"] is False

    def test_single_file_ci_from_inline_flag(self, tmp_path):
        """Inline (?i) flag should set effective CI even without the arg."""
        f = tmp_path / "t.txt"
        f.write_text("Hello\nHELLO\nhello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="(?i)hello")
        assert data["ok"] is True
        assert data["count"] == 3
        assert data["case_insensitive"] is True


# ------------------------------------------------------------------
# 2. Single file — binary / encoding / size
# ------------------------------------------------------------------

class TestSingleFileRejections:
    def test_binary_file_skipped(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"\x00\x01\x02\x03" * 100)
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="anything")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["scan_complete"] is True
        assert "stop_reason" not in data

    def test_encoding_fail_skipped(self, tmp_path):
        f = tmp_path / "bad.txt"
        f.write_bytes(b"\xc0\xc0\xc0\xc0" * 50 + b"hello")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="hello")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["scan_complete"] is True

    def test_file_too_large(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_bytes(b"A" * (16 * 1024 * 1024 + 1))
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="A")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["stop_reason"] == "file_too_large"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is False
        assert data["guidance"] is not None
        assert "size limit" in data["guidance"].lower() or "chunks" in data["guidance"]


# ------------------------------------------------------------------
# 3. Directory — basic search, A/B/C
# ------------------------------------------------------------------

class TestDirectoryBasic:
    def test_dir_basic_match(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "target line\n",
            "b.py": "nothing here\n",
            "c.py": "target again\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["mode"] == "dir"
        assert data["scan_complete"] is True
        assert "stop_reason" not in data
        assert "excluded_dirs" not in data
        _assert_slim_success_keys(data)

    def test_dir_no_match_scan_complete_B(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "foo\n",
            "b.py": "bar\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern=r"\w+")
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["scan_complete"] is True
        assert data["more_matches_may_exist"] is False
        assert "stop_reason" not in data

    def test_dir_prefilter_skips_files(self, tmp_path):
        """Prefilter with literal needle skips files that don't contain it."""
        _make_tree(tmp_path, {
            "a.py": "foo\n",
            "b.py": "bar\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="zzz")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["scan_complete"] is True
        assert data["more_matches_may_exist"] is False
        assert "stop_reason" not in data

    def test_dir_match_limit_A(self, tmp_path):
        files = {f"f{i}.py": "HIT\n" for i in range(20)}
        _make_tree(tmp_path, files)
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="HIT", limit=5)
        assert data["ok"] is True
        assert data["count"] == 5
        assert data["stop_reason"] == "match_limit"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data["limit_eff"] == 5
        assert "limit_requested" not in data
        assert data["guidance"] is not None
        # no breakdown on match_limit
        assert "top_level_dirs" not in data
        assert "candidate_count" not in data

    def test_dir_file_pattern_filter(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.md": "needle\n",
            "c.txt": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="*.py",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["file_pattern"] == "*.py"
        assert data["matches"][0]["file"].endswith("a.py")

    def test_dir_limit_capped(self, tmp_path):
        files = {f"f{i}.py": "HIT\n" for i in range(10)}
        _make_tree(tmp_path, files)
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="HIT",
            limit=500,
        )
        assert data["ok"] is True
        assert data["limit_eff"] == 300
        assert data["limit_capped"] is True
        assert data["limit_requested"] == 500
        assert data["count"] == 10
        assert data["stop_reason"] == "requested_limit_capped"


# ------------------------------------------------------------------
# 4. Directory — excluded_dirs (opt-in show_excluded_dirs)
# ------------------------------------------------------------------

class TestExcludedDirs:
    def test_excluded_dir_names_venv(self, tmp_path):
        _make_tree(tmp_path, {
            "main.py": "target\n",
            "venv/lib.py": "target\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            show_excluded_dirs=True,
        )
        assert data["ok"] is True
        assert data["count"] == 1
        excluded = data["excluded_dirs"]
        assert len(excluded) == 1
        assert excluded[0]["reason"] == "excluded_directory_name"
        assert "venv" in excluded[0]["path"]

    def test_excluded_dir_names_pycache(self, tmp_path):
        _make_tree(tmp_path, {
            "code.py": "target\n",
            "__pycache__/cache.py": "target\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            show_excluded_dirs=True,
        )
        excluded = data["excluded_dirs"]
        assert len(excluded) == 1
        assert excluded[0]["reason"] == "excluded_directory_name"
        assert "__pycache__" in excluded[0]["path"]
        assert data["count"] == 1

    def test_excluded_dot_dir(self, tmp_path):
        _make_tree(tmp_path, {
            "main.py": "target\n",
            ".hidden/secret.py": "target\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            show_excluded_dirs=True,
        )
        excluded = data["excluded_dirs"]
        assert len(excluded) == 1
        assert excluded[0]["reason"] == "hidden_dot_directory"
        assert ".hidden" in excluded[0]["path"]
        assert data["count"] == 1

    def test_excluded_multiple_dirs(self, tmp_path):
        _make_tree(tmp_path, {
            "main.py": "target\n",
            "venv/lib.py": "target\n",
            "__pycache__/c.py": "target\n",
            ".git/config": "target\n",
            ".cache/x.py": "target\n",
            "node_modules/pkg.py": "target\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            show_excluded_dirs=True,
        )
        excluded = data["excluded_dirs"]
        reasons = {e["reason"] for e in excluded}
        assert "excluded_directory_name" in reasons
        assert "hidden_dot_directory" in reasons
        assert len(excluded) == 5
        assert data["count"] == 1
        # sorted by reason, then path
        paths = [e["path"] for e in excluded]
        assert paths == sorted(paths, key=lambda p: (
            next(e["reason"] for e in excluded if e["path"] == p),
            p,
        ))

    def test_excluded_dirs_default_omitted(self, tmp_path):
        _make_tree(tmp_path, {
            "main.py": "target\n",
            "venv/lib.py": "target\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
        assert data["ok"] is True
        assert data["count"] == 1
        assert "excluded_dirs" not in data

    def test_hard_floor_survives_empty_config_exclude_list(self, tmp_path):
        """Clearing fs_tools.find_in_files.exclude_dir_names must not re-open junk."""
        _make_tree(tmp_path, {
            "main.py": "target\n",
            "venv/lib.py": "target\n",
            "node_modules/pkg.py": "target\n",
        })
        cfg = {
            "fs_tools": {
                "find_in_files": {
                    "exclude_dir_names": [],
                    "exclude_dot_dirs": False,
                }
            }
        }
        data = _call(
            with_local_ctx(FindInFiles, config_dict=cfg),
            path=str(tmp_path),
            pattern="target",
            query_mode="text",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        paths = {m["file"] for m in data["matches"]}
        assert any(p.endswith("main.py") for p in paths)
        assert not any("venv" in p or "node_modules" in p for p in paths)

    def test_start_path_not_excluded(self, tmp_path):
        """If path itself is an excluded name (e.g. venv/), search inside."""
        venv = tmp_path / "venv"
        _make_tree(tmp_path, {
            "venv/lib.py": "target\n",
            "venv/sub/deep.py": "target\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(venv), pattern="target")
        assert data["ok"] is True
        assert data["count"] == 2

    def test_dot_file_in_regular_dir_searched(self, tmp_path):
        """.env files in normal dirs ARE searchable (only dirs excluded)."""
        _make_tree(tmp_path, {
            ".env": "SECRET=target\n",
            "main.py": "target\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
        assert data["ok"] is True
        assert data["count"] == 2


# ------------------------------------------------------------------
# 5. Directory — sorting (basename_needle,depth,path)
# ------------------------------------------------------------------

class TestSortOrder:
    def test_basename_hit_priority(self, tmp_path):
        """File with needle in basename should be scanned first."""
        _make_tree(tmp_path, {
            "zzz.py": "needle\n",
            "needle.py": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            limit=1,
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("needle.py")

    def test_depth_priority(self, tmp_path):
        """Shallower files are scanned before deeper ones (same basename_hit)."""
        _make_tree(tmp_path, {
            "shallow.py": "HIT\n",
            "deep/a/b/c.py": "HIT\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="HIT",
            limit=1,
        )
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("shallow.py")


# ------------------------------------------------------------------
# 6. Directory — limits & stop reasons
# ------------------------------------------------------------------

class TestDirectoryLimits:
    def test_too_many_candidate_files(self, tmp_path):
        """N > L(mode) → too_many_candidate_files + breakdown; content not read.

        Native LIKE/text skips this ceiling (straight to grep). Force the
        Python path via show_excluded_dirs — that is where the guard lives.
        """
        cfg = {
            "fs_tools": {
                "find_in_files": {
                    "max_candidates_like": 5,
                }
            }
        }
        _make_tree(
            tmp_path,
            {
                **{f"f{i}.py": "x\n" for i in range(6)},
                "sub_a/a.py": "x\n",
                "sub_a/b.py": "x\n",
                "sub_b/c.py": "x\n",
                "sub_b/d.py": "x\n",
            },
        )
        data = _call(
            with_local_ctx(FindInFiles, config_dict=cfg),
            path=str(tmp_path),
            pattern="x",
            query_mode="text",
            show_excluded_dirs=True,
        )
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["stop_reason"] == "too_many_candidate_files"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data["candidate_count"] == 10
        assert data["candidate_limit"] == 5
        assert data["files_at_root"] == 6
        assert "top_level_dirs" in data
        assert data["top_level_dirs_total"] == 2
        assert data["top_level_dirs_shown"] == 2
        assert data["top_level_dirs_shown"] == len(data["top_level_dirs"])
        # sort desc by candidate_count
        counts = [t["candidate_count"] for t in data["top_level_dirs"]]
        assert counts == sorted(counts, reverse=True)
        assert "content was not searched" in data["guidance"].lower()
        assert "path=" in data["guidance"]
        # no needles / stats / exclude echo
        assert "needles" not in data
        assert "exclude_dirs" not in data
        assert "debug_info" not in data

    def test_complex_refuse_guidance(self, tmp_path):
        """complex over cap → same stop_reason + guidance mentions simplify/text."""
        cfg = {
            "fs_tools": {
                "find_in_files": {
                    "max_candidates_complex": 3,
                }
            }
        }
        _make_tree(tmp_path, {f"f{i}.py": "word\n" for i in range(5)})
        data = _call(
            with_local_ctx(FindInFiles, config_dict=cfg),
            path=str(tmp_path),
            pattern=r"\w+",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["stop_reason"] == "too_many_candidate_files"
        assert data["candidate_limit"] == 3
        assert data["candidate_count"] == 5
        g = data["guidance"].lower()
        assert "simplify" in g or "query_mode=text" in g
        # all files at root → no child dirs in breakdown
        assert "top_level_dirs" not in data
        assert "top_level_dirs_shown" not in data
        assert "top_level_dirs_total" not in data
        assert data["stop_reason"] != "pattern_too_broad"

    def test_refuse_top_level_dirs_cap_5(self, tmp_path):
        """top_level_dirs capped at 5 with shown/total.

        Python-path refuse only: native text search no longer counts first.
        """
        cfg = {
            "fs_tools": {
                "find_in_files": {
                    "max_candidates_like": 2,
                    "top_level_dirs_cap": 5,
                }
            }
        }
        files = {}
        # 8 top-level dirs with 1 file each + 3 root files → 11 candidates
        for i in range(8):
            files[f"d{i}/f.py"] = "x\n"
        for i in range(3):
            files[f"root{i}.py"] = "x\n"
        _make_tree(tmp_path, files)
        data = _call(
            with_local_ctx(FindInFiles, config_dict=cfg),
            path=str(tmp_path),
            pattern="x",
            query_mode="text",
            show_excluded_dirs=True,
        )
        assert data["stop_reason"] == "too_many_candidate_files"
        assert data["top_level_dirs_total"] == 8
        assert data["top_level_dirs_shown"] == 5
        assert len(data["top_level_dirs"]) == 5
        assert "Showing top 5 of 8" in data["guidance"]

    def test_complete_scan_no_top_level_dirs(self, tmp_path):
        """Complete content scan: no breakdown / top_level_dirs."""
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "sub/b.py": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            query_mode="text",
        )
        assert data["ok"] is True
        assert data["scan_complete"] is True
        assert "top_level_dirs" not in data
        assert "candidate_count" not in data

    def test_partial_match_limit_no_breakdown(self, tmp_path):
        """Partial (match_limit): no top_level_dirs breakdown."""
        files = {f"f{i}.py": "HIT\n" for i in range(10)}
        _make_tree(tmp_path, files)
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="HIT",
            query_mode="text",
            limit=3,
        )
        assert data["stop_reason"] == "match_limit"
        assert data["scan_complete"] is False
        assert "top_level_dirs" not in data
        assert "candidate_count" not in data
        assert data["limit_eff"] == 3

    def test_too_many_files_opened_C_affix_or(self, tmp_path):
        """affix_or re-open budget → too_many_files_opened (C)."""
        cfg = {
            "fs_tools": {
                "find_in_files": {
                    "max_open_files_affix_or": 3,
                }
            }
        }
        _make_tree(tmp_path, {f"f{i}.py": "def foo\n" for i in range(10)})
        data = _call(
            with_local_ctx(FindInFiles, config_dict=cfg),
            path=str(tmp_path),
            pattern=r"def (foo|bar)",
            query_mode="python-re",
            limit=100,
        )
        assert data["ok"] is True
        assert data["stop_reason"] == "too_many_files_opened"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data["guidance"] is not None
        assert "partial" in data["guidance"].lower() or "budget" in data["guidance"].lower()
        # no breakdown on open budget (v1)
        assert "top_level_dirs" not in data

    def test_A_boundary_exact_limit(self, tmp_path):
        """Last candidate gives exactly limit_eff → A (not B)."""
        _make_tree(tmp_path, {
            "a.py": "HIT\n",
            "b.py": "HIT\n",
            "c.py": "HIT\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="HIT", limit=3)
        assert data["ok"] is True
        assert data["count"] == 3
        assert data["stop_reason"] == "match_limit"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True

    def test_B_complete_no_stop(self, tmp_path):
        """B outcome: full scan, no stop_reason."""
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.py": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="needle")
        assert data["ok"] is True
        assert "stop_reason" not in data
        assert data["scan_complete"] is True
        assert data["count"] == 2


# ------------------------------------------------------------------
# 7. Prefilter / needles through the API (behavior only)
# ------------------------------------------------------------------

class TestPrefilterNeedles:
    def test_like_literal_prefilter(self, tmp_path):
        """like mode: required_literal skips files without the substring."""
        _make_tree(tmp_path, {
            "a.py": "foo bar baz\n",
            "b.py": "only bar\n",
            "c.py": "nothing\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="foo")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("a.py")

    def test_or_group_prefilter(self, tmp_path):
        """OR-group in pattern → files containing any branch pass prefilter."""
        _make_tree(tmp_path, {
            "a.py": "apple\n",
            "b.py": "banana\n",
            "c.py": "cherry\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern=r"(apple|banana)",
        )
        assert data["ok"] is True
        assert data["count"] == 2
        files = {m["file"].rsplit("/", 1)[-1] for m in data["matches"]}
        assert files == {"a.py", "b.py"}

    def test_no_prefilter_pattern(self, tmp_path):
        """Pattern with no literal needles (e.g. \\d+) has no prefilter."""
        _make_tree(tmp_path, {
            "a.py": "123\n",
            "b.py": "abc\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern=r"\d+")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("a.py")

    def test_prefilter_case_insensitive(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "NEEDLE found\n",
            "b.py": "needle found\n",
            "c.py": "no match\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            case_insensitive=True,
        )
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["case_insensitive"] is True

    def test_prefilter_case_sensitive(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "NEEDLE\n",
            "b.py": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            case_insensitive=False,
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("b.py")
        assert data["case_insensitive"] is False

    def test_top_level_or_outside_group_is_not_prefiltered(self, tmp_path):
        """``foo(bar|baz)|qux`` does not require ``foo`` — prefilter must not drop the file."""
        _make_tree(tmp_path, {"a.py": "qux only here\n"})

        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="foo(bar|baz)|qux",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("a.py")

        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path / "a.py"),
            pattern="foo(bar|baz)|qux",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["count"] == 1

    def test_optional_or_group_does_not_gate_the_prefilter(self, tmp_path):
        """``foo(bar|baz)?`` — группа необязательна, достаточно required-литерала.

        Правило сформулировано в самом файле рядом с ``_match_decoded_text``:
        "optional or_group does not skip re (soundness of required alone)".
        """
        _make_tree(tmp_path, {"a.py": "fooqux here\n"})

        for pattern in ("foo(bar|baz)?", "foo(bar|baz)*", "foo(bar|baz){0,2}"):
            data = _call(
                with_local_ctx(FindInFiles),
                path=str(tmp_path),
                pattern=pattern,
                query_mode="python-re",
            )
            assert data["ok"] is True, pattern
            assert data["count"] == 1, pattern

    def test_required_literal_still_gates_the_prefilter(self, tmp_path):
        """Обязательная часть по-прежнему отсекает файл без неё."""
        _make_tree(tmp_path, {"a.py": "nothing relevant here\n"})

        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="foo(bar|baz)?",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["count"] == 0


# ------------------------------------------------------------------
# 8. Error envelopes
# ------------------------------------------------------------------

class TestErrorEnvelopes:
    def test_path_not_found(self, tmp_path):
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path / "nonexistent"),
            pattern="x",
        )
        assert data["ok"] is False
        assert "error" in data
        assert data["error"]["type"] == "not_found"
        assert data["matches"] == []
        assert data["count"] == 0

    def test_relative_path_error(self, tmp_path):
        data = _call(
            with_local_ctx(FindInFiles),
            path="relative/path",
            pattern="x",
            query_mode="text",
        )
        assert data["ok"] is False
        assert data["error"]["type"] == "invalid_argument"
        assert data["error"]["field"] == "path"

    def test_empty_pattern_rejected_in_python_re(self, tmp_path):
        """Empty re is zero-width everywhere — reject before scan flood."""
        f = tmp_path / "afile"
        f.write_text("hi\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="",
            query_mode="python-re",
        )
        assert data["ok"] is False
        assert data["error"]["type"] == "invalid_argument"
        assert data["error"]["field"] == "pattern"

    def test_empty_pattern_rejected_when_query_mode_omitted(self, tmp_path):
        f = tmp_path / "afile"
        f.write_text("hi\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="",
        )
        assert data["ok"] is False
        assert data["error"]["field"] == "pattern"

    def test_path_is_neither_file_nor_dir(self, tmp_path):
        fifo = tmp_path / "pipe"
        os.mkfifo(str(fifo))
        data = _call(with_local_ctx(FindInFiles), path=str(fifo), pattern="x")
        assert data["ok"] is False
        assert data["error"]["type"] == "not_a_directory"

    def test_bad_regex(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern=r"[unclosed")
        assert data["ok"] is False
        assert data["error"]["type"] == "invalid_argument"
        assert data["error"]["field"] == "pattern"

    def test_missing_path(self, tmp_path):
        with pytest.raises(TypeError):
            _call(with_local_ctx(FindInFiles), pattern="x")

    def test_missing_pattern(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello\n", encoding="utf-8")
        with pytest.raises(TypeError):
            _call(with_local_ctx(FindInFiles), path=str(f))

    def test_negative_limit(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="hello",
            limit=-5,
        )
        assert data["ok"] is False
        assert data["error"]["type"] == "invalid_argument"


# ------------------------------------------------------------------
# exclude_dirs (call-time)
# ------------------------------------------------------------------

class TestExcludeDirsArg:
    def test_exclude_dirs_skips_subtree(self, tmp_path):
        _make_tree(tmp_path, {
            "keep/a.py": "target\n",
            "skip/b.py": "target\n",
            "skip/nested/c.py": "target\n",
        })
        skip = str(tmp_path / "skip")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            query_mode="text",
            exclude_dirs=[skip],
            show_excluded_dirs=True,
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("a.py")
        # do not echo accepted exclude_dirs
        assert "exclude_dirs" not in data
        reasons = {e["reason"] for e in data["excluded_dirs"]}
        assert "call_exclude_dirs" in reasons

    def test_exclude_dirs_relative_error(self, tmp_path):
        _make_tree(tmp_path, {"a.py": "x\n"})
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="x",
            query_mode="text",
            exclude_dirs=["relative"],
        )
        assert data["ok"] is False
        assert data["error"]["field"] == "exclude_dirs"

    def test_exclude_dirs_start_path_error(self, tmp_path):
        _make_tree(tmp_path, {"a.py": "x\n"})
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="x",
            query_mode="text",
            exclude_dirs=[str(tmp_path)],
        )
        assert data["ok"] is False
        assert data["error"]["field"] == "exclude_dirs"

    def test_exclude_dirs_missing_soft(self, tmp_path):
        _make_tree(tmp_path, {"a.py": "x\n"})
        missing = str(tmp_path / "no_such_dir")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="x",
            query_mode="text",
            exclude_dirs=[missing],
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data.get("exclude_dir_errors")
        assert data["exclude_dir_errors"][0]["reason"] == "missing"
        assert "exclude_dirs" not in data

    def test_exclude_dirs_too_many(self, tmp_path):
        _make_tree(tmp_path, {"a.py": "x\n"})
        dirs = []
        for i in range(33):
            d = tmp_path / f"e{i}"
            d.mkdir()
            dirs.append(str(d))
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="x",
            query_mode="text",
            exclude_dirs=dirs,
        )
        assert data["ok"] is False
        assert data["error"]["field"] == "exclude_dirs"


# ------------------------------------------------------------------
# 9. Case-insensitive in directory mode
# ------------------------------------------------------------------

class TestCaseInsensitiveDirectory:
    def test_ci_dir_match(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "Hello World\n",
            "b.py": "HELLO there\n",
            "c.py": "hello\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="hello",
            case_insensitive=True,
        )
        assert data["ok"] is True
        assert data["count"] == 3
        assert data["case_insensitive"] is True

    def test_cs_dir_match(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "Hello\n",
            "b.py": "hello\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="hello",
            case_insensitive=False,
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("b.py")
        assert data["case_insensitive"] is False

    def test_ci_from_inline_flag_dir(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "Hello\n",
            "b.py": "hello\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="(?i)hello",
        )
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["case_insensitive"] is True


# ------------------------------------------------------------------
# 10. file_pattern edge cases
# ------------------------------------------------------------------

class TestFilePatternEdge:
    def test_file_pattern_py_only(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.txt": "needle\n",
            "c.md": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="*.py",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["file_pattern"] == "*.py"
        assert data["matches"][0]["file"].endswith("a.py")

    def test_file_pattern_txt_only(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.txt": "needle\n",
            "c.md": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="*.txt",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("b.txt")

    def test_file_pattern_no_match(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.txt": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="*.rs",
        )
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["scan_complete"] is True
        assert data["file_pattern"] == "*.rs"

    def test_file_pattern_in_subdirs(self, tmp_path):
        _make_tree(tmp_path, {
            "src/a.py": "needle\n",
            "src/b.txt": "needle\n",
            "tests/c.py": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="**/*.py",
        )
        assert data["ok"] is True
        assert data["count"] == 2

    def test_file_pattern_basename_star_is_recursive(self, tmp_path):
        """Basename-only '*.py' matches at any depth (parity with name-locate)."""
        _make_tree(tmp_path, {
            "root.py": "needle\n",
            "src/a.py": "needle\n",
            "src/deep/b.py": "needle\n",
            "src/a.txt": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="*.py",
        )
        assert data["ok"] is True
        assert data["count"] == 3
        names = sorted(Path(m["file"]).name for m in data["matches"])
        assert names == ["a.py", "b.py", "root.py"]

    def test_file_pattern_with_slash_stays_one_level(self, tmp_path):
        """Path globs with '/' keep single-segment '*' semantics."""
        _make_tree(tmp_path, {
            "src/a.py": "needle\n",
            "src/deep/b.py": "needle\n",
            "other/c.py": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="src/*.py",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("src/a.py") or data["matches"][0][
            "file"
        ].endswith("src\\a.py")

    def test_file_pattern_all_files(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.txt": "needle\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
            file_pattern="*",
        )
        assert data["ok"] is True
        assert data["count"] == 2

    def test_no_file_pattern_key_when_omitted(self, tmp_path):
        _make_tree(tmp_path, {"a.py": "needle\n"})
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="needle",
        )
        assert "file_pattern" not in data


# ------------------------------------------------------------------
# 11. Match format — line / end_line / line_content / context
# ------------------------------------------------------------------

class TestMatchFormat:
    def test_match_has_required_fields(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("line1\nneedle\nline3\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        assert data["count"] == 1
        m = data["matches"][0]
        assert "file" in m
        assert "line" in m
        assert "end_line" in m
        assert "line_content" in m
        assert "context" in m

    def test_line_content_single_line(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("alpha\nneedle here\nbeta\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        m = data["matches"][0]
        assert m["line"] == 2
        assert m["end_line"] == 2
        assert m["line_content"] == "needle here"

    def test_line_content_is_full_start_line_not_match_span(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("    def foo(bar):\n        return 1\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="def ")
        m = data["matches"][0]
        assert m["line_content"] == "    def foo(bar):"

    def test_line_content_head_truncate_when_match_in_prefix(self, tmp_path):
        # Match near the start; long tail → plain head clip (match still visible).
        long_line = "needle " + ("y" * 600)
        f = tmp_path / "t.py"
        f.write_text(long_line + "\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        m = data["matches"][0]
        assert m["line_content"].startswith("needle ")
        assert m["line_content"].endswith(
            f"...[line_truncated: showing first 500 of {len(long_line)} chars]"
        )
        assert m["line_content"].startswith(long_line[:500])

    def test_line_content_window_around_match_when_past_head(self, tmp_path):
        # Match after char 500 → window with ±50 around match, not a blind head prefix.
        long_line = ("x" * 600) + "needle" + ("y" * 80)
        f = tmp_path / "t.py"
        f.write_text(long_line + "\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        m = data["matches"][0]
        lc = m["line_content"]
        assert "needle" in lc
        assert lc.startswith("...")
        assert lc.endswith(f"[line_truncated: total {len(long_line)} chars]")
        # ±50 pad around "needle" at offset 600
        assert ("x" * 50) + "needle" + ("y" * 50) in lc
        # Window, not a second head dump: only one pad of x's before match.
        assert lc.count("x") == 50

    def test_line_content_window_near_line_end(self, tmp_path):
        long_line = ("a" * 700) + "TAILMATCH"
        f = tmp_path / "t.py"
        f.write_text(long_line + "\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="TAILMATCH")
        lc = data["matches"][0]["line_content"]
        assert lc.startswith("...")
        assert "TAILMATCH" in lc
        # Match at EOL → no trailing "..." before the total marker.
        assert lc.endswith(f"TAILMATCH[line_truncated: total {len(long_line)} chars]")

    def test_multiline_match_line_content_is_start_line_only(self, tmp_path):
        f = tmp_path / "t.md"
        f.write_text("start here\nblock1\nblock2\nend\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern=r"start.*end",
            query_mode="python-re-dotall",
        )
        m = data["matches"][0]
        assert m["line"] == 1
        assert m["end_line"] == 4
        assert m["line_content"] == "start here"

    def test_multiline_match_end_line(self, tmp_path):
        f = tmp_path / "t.md"
        f.write_text("start\nblock1\nblock2\nend\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern=r"start.*end",
            query_mode="python-re-dotall",
        )
        assert data["count"] == 1
        m = data["matches"][0]
        assert m["line"] == 1
        assert m["end_line"] == 4

    def test_context_surrounds_match(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("l1\nl2\nl3\nneedle\nl5\nl6\nl7\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        m = data["matches"][0]
        assert "context" in m
        assert isinstance(m["context"], str)
        assert len(m["context"]) > 0

    def test_multiple_matches_line_numbers(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("a\nneedle\nb\nneedle\nc\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        assert data["count"] == 2
        assert [m["line"] for m in data["matches"]] == [2, 4]

    def test_match_at_file_start(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("needle\nline2\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        assert data["matches"][0]["line"] == 1

    def test_match_at_file_end(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("line1\nneedle\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        assert data["matches"][0]["line"] == 2


# ------------------------------------------------------------------
# 12. Slim limits echo
# ------------------------------------------------------------------

class TestSlimLimits:
    def test_quiet_scan_omits_limits(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("x\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="x", limit=50)
        assert data["ok"] is True
        assert "limit_requested" not in data
        assert "limit_eff" not in data
        assert "limit_capped" not in data

    def test_match_limit_has_limit_eff_only(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("x\n" * 20, encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="x", limit=5)
        assert data["stop_reason"] == "match_limit"
        assert data["limit_eff"] == 5
        assert "limit_requested" not in data
        assert "limit_capped" not in data


# ------------------------------------------------------------------
# query_mode: text vs python-re vs python-re-dotall
# ------------------------------------------------------------------

class TestQueryMode:
    def test_text_matches_parens_literally(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def foo(bar):\n    return 1\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="def foo(bar)",
            query_mode="text",
        )
        assert data["ok"] is True
        assert data["query_mode"] == "text"
        assert data["count"] == 1
        assert data["matches"][0]["line_content"] == "def foo(bar):"

    def test_regexp_parens_are_group_not_literal(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def foo(bar):\ndef foobar:\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="def foo(bar)",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["query_mode"] == "python-re"
        assert data["count"] == 1
        assert data["matches"][0]["line_content"] == "def foobar:"

    def test_text_dot_is_literal(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("a.b\naxb\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="a.b",
            query_mode="text",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["line_content"] == "a.b"

    def test_regexp_dot_is_any_char(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("a.b\naxb\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="a.b",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["count"] == 2

    def test_text_case_insensitive(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("Foo(Bar)\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="foo(bar)",
            query_mode="text",
            case_insensitive=True,
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["case_insensitive"] is True

    def test_text_empty_pattern_error(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("x\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="",
            query_mode="text",
        )
        assert data["ok"] is False
        assert data["error"]["type"] == "invalid_argument"
        assert data["error"]["field"] == "pattern"
        assert "non-empty" in data["error"]["message"]

    def test_query_mode_omitted_defaults_to_python_re(self, tmp_path):
        """Omit query_mode → python-re (regex | / groups work)."""
        f = tmp_path / "harbor.py"
        f.write_text(
            "def is_hidden(path):\n"
            "    return False\n"
            "def iter_walk(root):\n"
            "    yield root\n",
            encoding="utf-8",
        )
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern=r"def (is_hidden|iter_walk|iter_files)",
        )
        assert data["ok"] is True
        assert data["query_mode"] == "python-re"
        assert data["count"] == 2
        lines = {m["line_content"] for m in data["matches"]}
        assert "def is_hidden(path):" in lines
        assert "def iter_walk(root):" in lines

    def test_query_mode_none_defaults_to_python_re(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("foo\nbar\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern=r"foo|bar",
            query_mode=None,
        )
        assert data["ok"] is True
        assert data["query_mode"] == "python-re"
        assert data["count"] == 2

    def test_invalid_query_mode(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("x\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="x",
            query_mode="glob",
        )
        assert data["ok"] is False
        assert data["error"]["field"] == "query_mode"

    def test_schema_query_mode_optional_default_python_re(self):
        schema = FindInFiles.tool_description()["parameters"]
        assert schema["required"] == ["path", "pattern"]
        assert "query_mode" not in schema["required"]
        qm = schema["properties"]["query_mode"]
        assert qm["enum"] == [
            "text",
            "python-re",
            "python-re-dotall",
        ]
        assert qm["default"] == "python-re"
        assert "exclude_dirs" in schema["properties"]
        assert "show_excluded_dirs" in schema["properties"]
        assert schema["properties"]["show_excluded_dirs"]["default"] is False
        assert "verbose" not in schema["properties"]
        assert schema.get("additionalProperties") is False

    def test_slim_payload_default(self, tmp_path):
        """Default: no debug/verbose; heavy fields not top-level."""
        _make_tree(tmp_path, {
            "a.py": "target line\n",
            "b.py": "nothing\n",
            "__pycache__/x.pyc": b"\x00\x01",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            query_mode="text",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert "stop_reason" not in data
        assert "guidance" not in data
        assert "excluded_dirs" not in data
        assert "exclude_dirs" not in data
        _assert_slim_success_keys(data)

    def test_show_excluded_dirs_opt_in(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "target\n",
            "venv/x.py": "target\n",
            "__pycache__/y.py": "target\n",
        })
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            query_mode="text",
            show_excluded_dirs=True,
        )
        assert data["ok"] is True
        assert "show_excluded_dirs" not in data
        assert "excluded_dirs" in data
        assert len(data["excluded_dirs"]) >= 2
        # full list, sorted reason/path; no shown/total
        assert "excluded_dirs_shown" not in data
        assert "excluded_dirs_total" not in data
        assert "search_stats" not in data
        assert "debug_info" not in data

    def test_show_excluded_dirs_empty_omitted(self, tmp_path):
        _make_tree(tmp_path, {"a.py": "target\n"})
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(tmp_path),
            pattern="target",
            query_mode="text",
            show_excluded_dirs=True,
        )
        assert data["ok"] is True
        assert "excluded_dirs" not in data

    def test_text_mode_works_like_literal(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="hello",
            query_mode="text",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["case_insensitive"] is False

    def test_regexp_or_simple_mode(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("foo\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="foo|bar",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["count"] == 1

    def test_python_re_dot_does_not_match_newline(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("error: something\nhappened\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern=r"error: .+",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["query_mode"] == "python-re"
        assert data["count"] == 1
        assert data["matches"][0]["line"] == 1
        assert data["matches"][0]["end_line"] == 1
        assert data["matches"][0]["line_content"] == "error: something"

    def test_python_re_dotall_dot_matches_newline(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("error: something\nhappened\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern=r"error: .+",
            query_mode="python-re-dotall",
        )
        assert data["ok"] is True
        assert data["query_mode"] == "python-re-dotall"
        assert data["count"] == 1
        m = data["matches"][0]
        assert m["line"] == 1
        assert m["end_line"] == 2
        # line_content is the start line only; span is in line/end_line + context
        assert m["line_content"] == "error: something"
        assert "happened" in m["context"]

    def test_python_re_inline_s_still_spans_lines(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("start\nmiddle\nend\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern=r"(?s)start.*end",
            query_mode="python-re",
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["end_line"] == 3
