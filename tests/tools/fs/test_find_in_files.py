"""Integration tests for fs_find_in_files.

Covers scan-automaton: mode caps, refuse breakdown, exclude_dirs,
single-file / directory, A/B/C, workers, query_mode, prefilter.
"""

import json
import os
import struct
import zlib

import pytest

from tools.fs.find_in_files import FindInFiles
from tests.helpers.tool_ctx import with_local_ctx


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _call(tool, **kw):
    """Call FindInFiles and parse JSON response.

    Default query_mode=python-re when omitted (older tests).
    """
    kw.setdefault("query_mode", "python-re")
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
        assert data["stop_reason"] is None
        assert all(m["file"].endswith("code.py") for m in data["matches"])
        assert [m["line"] for m in data["matches"]] == [1, 3]

    def test_single_file_no_match(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="zzz")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["matches"] == []
        assert data["scan_complete"] is True
        assert data["stop_reason"] is None
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
        assert data["limit_requested"] == 10

    def test_single_file_limit_capped(self, tmp_path):
        f = tmp_path / "many.py"
        f.write_text("X\n" * 500, encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="X", limit=500)
        assert data["ok"] is True
        assert data["limit_eff"] == 300
        assert data["limit_requested"] == 500
        assert data["limit_capped"] is True
        # 300 matches collected (match_limit), also capped
        assert data["count"] == 300
        assert data["stop_reason"] == "match_limit"

    def test_single_file_limit_capped_no_match(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="zzz", limit=500)
        assert data["ok"] is True
        assert data["limit_eff"] == 300
        assert data["limit_capped"] is True
        assert data["stop_reason"] == "requested_limit_capped"
        assert data["count"] == 0
        assert data["scan_complete"] is True

    def test_single_file_case_insensitive(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("Hello\nHELLO\nhello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="hello", case_insensitive=True)
        assert data["ok"] is True
        assert data["count"] == 3
        assert data["search_stats"]["effective_ci"] is True
        assert data["search_stats"]["case_insensitive_arg"] is True

    def test_single_file_case_sensitive(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("Hello\nHELLO\nhello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="hello")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["search_stats"]["effective_ci"] is False

    def test_single_file_ci_from_inline_flag(self, tmp_path):
        """Inline (?i) flag should set effective_ci even without the arg."""
        f = tmp_path / "t.txt"
        f.write_text("Hello\nHELLO\nhello\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="(?i)hello")
        assert data["ok"] is True
        assert data["count"] == 3
        assert data["search_stats"]["effective_ci"] is True
        assert data["search_stats"]["case_insensitive_arg"] is False


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
        assert data["search_stats"]["skipped_binary"] == 1
        assert data["search_stats"]["skipped_encoding"] == 0

    def test_encoding_fail_skipped(self, tmp_path):
        f = tmp_path / "bad.txt"
        # Invalid UTF-8 continuation bytes without valid starts — not classified
        # as binary by the head sample, but fails full decode.
        f.write_bytes(b"\xc0\xc0\xc0\xc0" * 50 + b"hello")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="hello")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["search_stats"]["skipped_binary"] == 0
        assert data["search_stats"]["skipped_encoding"] == 1
        assert data["scan_complete"] is True

    def test_file_too_large(self, tmp_path):
        f = tmp_path / "big.txt"
        # Write > 16 MiB
        f.write_bytes(b"A" * (16 * 1024 * 1024 + 1))
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="A")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["stop_reason"] == "file_too_large"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is False
        assert data["stop_summary"] is not None
        assert data["guidance"] is not None


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
        assert data["excluded_dirs"] == []
        assert data["search_stats"]["files_walked"] == 3
        assert data["search_stats"]["candidates"] == 3
        assert data["search_stats"]["content_paths_touched"] == 3
        assert data["scan_complete"] is True
        assert data["stop_reason"] is None

    def test_dir_no_match_scan_complete_B(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "foo\n",
            "b.py": "bar\n",
        })
        # Use a pattern with no literal needles so files reach full regex
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern=r"\w+")
        assert data["ok"] is True
        assert data["count"] == 2  # both files match \w+
        assert data["scan_complete"] is True
        assert data["more_matches_may_exist"] is False
        assert data["stop_reason"] is None
        # B outcome: all candidates scanned with regex
        assert data["search_stats"]["files_opened_regex"] == 2

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
        assert data["stop_reason"] is None
        # prefilter: no file contains "zzz" → none opened for regex
        assert data["search_stats"]["files_opened_regex"] == 0
        assert data["search_stats"]["shortlist_len"] == 0

    def test_dir_match_limit_A(self, tmp_path):
        files = {f"f{i}.py": "HIT\n" for i in range(20)}
        _make_tree(tmp_path, files)
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="HIT", limit=5)
        assert data["ok"] is True
        assert data["count"] == 5
        assert data["stop_reason"] == "match_limit"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data["stop_summary"] is not None
        assert data["guidance"] is not None

    def test_dir_file_pattern_filter(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.md": "needle\n",
            "c.txt": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="needle", file_pattern="*.py")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["search_stats"]["after_glob"] == 1
        assert data["matches"][0]["file"].endswith("a.py")

    def test_dir_limit_capped(self, tmp_path):
        files = {f"f{i}.py": "HIT\n" for i in range(10)}
        _make_tree(tmp_path, files)
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="HIT", limit=500)
        assert data["ok"] is True
        assert data["limit_eff"] == 300
        assert data["limit_capped"] is True
        # Only 10 matches → B outcome, but cap noted
        assert data["count"] == 10
        # B outcome: stop_reason is requested_limit_capped (cap only, no match_limit)
        assert data["stop_reason"] == "requested_limit_capped"


# ------------------------------------------------------------------
# 4. Directory — excluded_dirs
# ------------------------------------------------------------------

class TestExcludedDirs:
    def test_excluded_dir_names_venv(self, tmp_path):
        _make_tree(tmp_path, {
            "main.py": "target\n",
            "venv/lib.py": "target\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
        assert data["ok"] is True
        # venv excluded
        excluded = data["excluded_dirs"]
        assert len(excluded) == 1
        assert excluded[0]["reason"] == "excluded_directory_name"
        assert "venv" in excluded[0]["path"]
        # Only main.py scanned
        assert data["count"] == 1

    def test_excluded_dir_names_pycache(self, tmp_path):
        _make_tree(tmp_path, {
            "code.py": "target\n",
            "__pycache__/cache.py": "target\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
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
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
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
            ".git/config": "target\n",  # hard floor basename (.git)
            ".cache/x.py": "target\n",  # nested dot-dir (not in name floor)
            "node_modules/pkg.py": "target\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
        excluded = data["excluded_dirs"]
        reasons = {e["reason"] for e in excluded}
        assert "excluded_directory_name" in reasons
        assert "hidden_dot_directory" in reasons
        # venv, __pycache__, .git, node_modules + .cache
        assert len(excluded) == 5
        assert data["count"] == 1  # only main.py

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
        # Start dir itself is not excluded
        assert data["count"] == 2
        assert all(e["reason"] != "excluded_directory_name" or "venv" not in e["path"].split("/")[-1] or e["path"] != str(venv) for e in data["excluded_dirs"])

    def test_dot_file_in_regular_dir_searched(self, tmp_path):
        """.env files in normal dirs ARE searchable (only dirs excluded)."""
        _make_tree(tmp_path, {
            ".env": "SECRET=target\n",
            "main.py": "target\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="target")
        assert data["ok"] is True
        assert data["count"] == 2  # .env and main.py


# ------------------------------------------------------------------
# 5. Directory — sorting (basename_needle,depth,path)
# ------------------------------------------------------------------

class TestSortOrder:
    def test_sort_order_constant(self, tmp_path):
        _make_tree(tmp_path, {"a.py": "needle\n"})
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="needle")
        assert data["search_stats"]["sort_order"] == "basename_needle,depth,path"

    def test_sort_order_single_file(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("needle\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        assert data["search_stats"]["sort_order"] == "single_file"

    def test_basename_hit_priority(self, tmp_path):
        """File with needle in basename should be scanned first."""
        _make_tree(tmp_path, {
            "zzz.py": "needle\n",       # no needle in basename
            "needle.py": "needle\n",    # needle in basename → higher priority
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="needle", limit=1)
        assert data["ok"] is True
        assert data["count"] == 1
        # needle.py should be found first (basename_hit=0)
        assert data["matches"][0]["file"].endswith("needle.py")

    def test_depth_priority(self, tmp_path):
        """Shallower files are scanned before deeper ones (same basename_hit)."""
        _make_tree(tmp_path, {
            "shallow.py": "HIT\n",
            "deep/a/b/c.py": "HIT\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="HIT", limit=1)
        assert data["count"] == 1
        # shallow.py (depth 0) before deep/.../c.py (depth 3)
        assert data["matches"][0]["file"].endswith("shallow.py")


# ------------------------------------------------------------------
# 6. Directory — limits & stop reasons
# ------------------------------------------------------------------

class TestDirectoryLimits:
    def test_too_many_candidate_files(self, tmp_path):
        """N > L(mode) → too_many_candidate_files + full breakdown; content not read."""
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
        )
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["stop_reason"] == "too_many_candidate_files"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data["stop_summary"] is not None
        assert "Content was not searched" in data["stop_summary"]
        assert data["guidance"] is not None
        assert data["candidate_count"] == 10
        assert data["candidate_limit"] == 5
        assert data["pattern_mode"] == "like"
        assert "top_level_dirs" in data
        assert data["files_at_root"] == 6
        assert data["top_level_dir_count"] == 2
        assert "needles" not in data or data.get("needles") is None
        assert data["search_stats"]["content_paths_touched"] == 0

    def test_complex_refuse_guidance(self, tmp_path):
        """complex over cap → same stop_reason + guidance mentions no-prefilter."""
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
        assert data["pattern_mode"] == "complex"
        assert data["candidate_limit"] == 3
        assert "prefilter" in data["guidance"].lower() or "simplify" in data["guidance"].lower()
        assert "top_level_dirs" in data
        # no pattern_too_broad
        assert data["stop_reason"] != "pattern_too_broad"

    def test_complete_scan_no_top_level_dirs(self, tmp_path):
        """Complete content scan: candidate_count only, no top_level_dirs."""
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
        assert data["candidate_count"] == 2
        assert "top_level_dirs" not in data
        assert data["content_workers"] == 1
        assert data.get("needles") is not None

    def test_partial_match_limit_has_breakdown(self, tmp_path):
        """Partial (match_limit): full breakdown present."""
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
        assert "top_level_dirs" in data
        assert data["candidate_count"] == 10

    def test_too_many_files_opened_C_affix_or(self, tmp_path):
        """affix_or re-open budget → too_many_files_opened (C)."""
        cfg = {
            "fs_tools": {
                "find_in_files": {
                    "max_open_files_affix_or": 3,
                }
            }
        }
        # affix_or pattern with required_literal "def " present in every file
        _make_tree(tmp_path, {f"f{i}.py": "def foo\n" for i in range(10)})
        data = _call(
            with_local_ctx(FindInFiles, config_dict=cfg),
            path=str(tmp_path),
            pattern=r"def (foo|bar)",
            query_mode="python-re",
            limit=100,
        )
        assert data["ok"] is True
        assert data["pattern_mode"] == "affix_or"
        assert data["stop_reason"] == "too_many_files_opened"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True
        assert data["search_stats"]["files_opened_regex"] == 3
        assert data["search_stats"]["max_open_files"] == 3
        assert data["search_stats"]["shortlist_len_is_lower_bound"] is True
        assert data["search_stats"]["coverage_ratio"] is None
        assert "top_level_dirs" in data
        assert data["stop_summary"] is not None
        assert data["guidance"] is not None

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
        # A boundary: exactly limit_eff reached → match_limit (A), not scan_complete B
        assert data["stop_reason"] == "match_limit"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True

    def test_B_coverage_ratio(self, tmp_path):
        """B outcome with shortlist_len > 0 → coverage_ratio = files_opened/shortlist."""
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.py": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="needle")
        assert data["ok"] is True
        assert data["stop_reason"] is None  # B outcome
        assert data["search_stats"]["shortlist_len_is_lower_bound"] is False
        assert data["search_stats"]["shortlist_len"] == 2
        assert data["search_stats"]["files_opened_regex"] == 2
        assert data["search_stats"]["coverage_ratio"] == 1.0




# ------------------------------------------------------------------
# 7. Prefilter / needles through the API
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
        assert data["search_stats"]["files_opened_regex"] == 1
        assert data["search_stats"]["and_needles"] == ["foo"]

    def test_or_group_prefilter(self, tmp_path):
        """OR-group in pattern → files containing any branch pass prefilter."""
        _make_tree(tmp_path, {
            "a.py": "apple\n",
            "b.py": "banana\n",
            "c.py": "cherry\n",   # neither apple nor banana
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern=r"(apple|banana)")
        assert data["ok"] is True
        assert data["count"] == 2
        files = {m["file"].rsplit("/", 1)[-1] for m in data["matches"]}
        assert files == {"a.py", "b.py"}
        assert data["search_stats"]["or_needles"] != []

    def test_no_prefilter_pattern(self, tmp_path):
        """Pattern with no literal needles (e.g. \\d+) has no prefilter."""
        _make_tree(tmp_path, {
            "a.py": "123\n",
            "b.py": "abc\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern=r"\d+")
        assert data["ok"] is True
        assert data["count"] == 1
        # no prefilter → all files opened for regex
        assert data["search_stats"]["files_opened_regex"] == 2
        assert data["search_stats"]["and_needles"] == []
        assert data["search_stats"]["or_needles"] == []

    def test_prefilter_case_insensitive(self, tmp_path):
        """Prefilter follows effective_ci: case_insensitive needle match."""
        _make_tree(tmp_path, {
            "a.py": "NEEDLE found\n",
            "b.py": "needle found\n",
            "c.py": "no match\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", case_insensitive=True)
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["search_stats"]["effective_ci"] is True

    def test_prefilter_case_sensitive(self, tmp_path):
        """Case-sensitive prefilter: only exact-case needle passes."""
        _make_tree(tmp_path, {
            "a.py": "NEEDLE\n",
            "b.py": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", case_insensitive=False)
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("b.py")
        assert data["search_stats"]["effective_ci"] is False


# ------------------------------------------------------------------
# 8. Error envelopes
# ------------------------------------------------------------------

class TestErrorEnvelopes:
    def test_path_not_found(self, tmp_path):
        data = _call(with_local_ctx(FindInFiles),
                      path=str(tmp_path / "nonexistent"), pattern="x")
        assert data["ok"] is False
        assert "error" in data
        assert data["error"]["type"] == "not_found"

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

    def test_empty_pattern_is_valid_regexp(self, tmp_path):
        """Empty pattern is a valid regex (matches empty string at every position)."""
        f = tmp_path / "afile"
        f.write_text("hi\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="",
            query_mode="python-re",
        )
        assert data["ok"] is True

    def test_path_is_neither_file_nor_dir(self, tmp_path):
        """A broken symlink or special path that exists but is not file/dir."""
        import os
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
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="hello", limit=-5)
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
        )
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("a.py")
        assert skip in data["exclude_dirs"] or any(
            "skip" in p for p in data.get("exclude_dirs", [])
        )
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
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="hello", case_insensitive=True)
        assert data["ok"] is True
        assert data["count"] == 3
        assert data["search_stats"]["effective_ci"] is True
        assert data["search_stats"]["case_insensitive_arg"] is True

    def test_cs_dir_match(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "Hello\n",
            "b.py": "hello\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="hello", case_insensitive=False)
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["matches"][0]["file"].endswith("b.py")
        assert data["search_stats"]["effective_ci"] is False

    def test_ci_from_inline_flag_dir(self, tmp_path):
        """Inline (?i) flag in pattern enables CI even without case_insensitive arg."""
        _make_tree(tmp_path, {
            "a.py": "Hello\n",
            "b.py": "hello\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="(?i)hello")
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["search_stats"]["effective_ci"] is True
        assert data["search_stats"]["case_insensitive_arg"] is False


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
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", file_pattern="*.py")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["search_stats"]["after_glob"] == 1
        assert data["matches"][0]["file"].endswith("a.py")

    def test_file_pattern_txt_only(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.txt": "needle\n",
            "c.md": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", file_pattern="*.txt")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["search_stats"]["after_glob"] == 1
        assert data["matches"][0]["file"].endswith("b.txt")

    def test_file_pattern_no_match(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.txt": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", file_pattern="*.rs")
        assert data["ok"] is True
        assert data["count"] == 0
        assert data["search_stats"]["after_glob"] == 0
        assert data["scan_complete"] is True

    def test_file_pattern_in_subdirs(self, tmp_path):
        _make_tree(tmp_path, {
            "src/a.py": "needle\n",
            "src/b.txt": "needle\n",
            "tests/c.py": "needle\n",
        })
        # '*.py' matches only files in root (rel_path has no '/');
        # '**/*.py' is needed for subdirectories (spec examples: '*.py', '**/*.md').
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", file_pattern="**/*.py")
        assert data["ok"] is True
        assert data["count"] == 2
        assert data["search_stats"]["after_glob"] == 2

    def test_file_pattern_star_dot_py_excludes_subdirs(self, tmp_path):
        """'*.py' must NOT match files inside subdirectories (glob applies to rel_path)."""
        _make_tree(tmp_path, {
            "root.py": "needle\n",
            "src/a.py": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", file_pattern="*.py")
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["search_stats"]["after_glob"] == 1
        assert data["matches"][0]["file"].endswith("root.py")

    def test_file_pattern_all_files(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.txt": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", file_pattern="*")
        assert data["ok"] is True
        assert data["count"] == 2


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
        """line_content = match.group(0) (matched text), not the full source line.
        Surrounding lines are available via `context`."""
        f = tmp_path / "t.py"
        f.write_text("alpha\nneedle here\nbeta\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        m = data["matches"][0]
        assert m["line"] == 2
        assert m["end_line"] == 2
        assert m["line_content"] == "needle"  # match.group(0), not whole line

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
        # context should contain surrounding lines
        assert "context" in m
        ctx = m["context"]
        # context is a structured field — just check it's a string and non-empty
        assert isinstance(ctx, str)
        assert len(ctx) > 0

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
        m = data["matches"][0]
        assert m["line"] == 1

    def test_match_at_file_end(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("line1\nneedle\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        m = data["matches"][0]
        assert m["line"] == 2


# ------------------------------------------------------------------
# 12. Search stats counters
# ------------------------------------------------------------------

class TestSearchStats:
    def test_stats_single_file(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("needle\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        stats = data["search_stats"]
        assert stats["files_walked"] == 1
        assert stats["after_glob"] == 1
        assert stats["after_size"] == 1
        assert stats["candidates"] == 1
        assert stats["content_paths_touched"] == 1
        assert stats["sort_order"] == "single_file"

    def test_stats_directory_counters(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.py": "nothing\n",
            "c.md": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path), pattern="needle")
        stats = data["search_stats"]
        assert stats["files_walked"] == 3
        assert stats["candidates"] == 3
        # all 3 files contain text; "needle" prefilter: a.py and c.md pass, b.py doesn't
        assert stats["shortlist_len"] == 2
        assert stats["files_opened_regex"] == 2

    def test_stats_file_pattern_reduces_candidates(self, tmp_path):
        _make_tree(tmp_path, {
            "a.py": "needle\n",
            "b.md": "needle\n",
            "c.py": "needle\n",
        })
        data = _call(with_local_ctx(FindInFiles), path=str(tmp_path),
                      pattern="needle", file_pattern="*.py")
        stats = data["search_stats"]
        assert stats["files_walked"] == 3
        assert stats["after_glob"] == 2
        assert stats["candidates"] == 2

    def test_stats_bytes_read(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("needle\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="needle")
        stats = data["search_stats"]
        assert stats["bytes_read"] > 0

    def test_stats_limit_eff_echo(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text("x\n", encoding="utf-8")
        data = _call(with_local_ctx(FindInFiles), path=str(f), pattern="x", limit=50)
        assert data["limit_requested"] == 50
        assert data["limit_eff"] == 50
        assert data["limit_capped"] is False


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
        assert data["matches"][0]["line_content"] == "def foo(bar)"
        assert data["search_stats"]["and_needles"] == ["def foo(bar)"]
        assert data["search_stats"]["query_mode"] == "text"

    def test_regexp_parens_are_group_not_literal(self, tmp_path):
        f = tmp_path / "code.py"
        # Literal parens in file; unescaped (bar) in regex is a group → matches "def foobar".
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
        assert data["matches"][0]["line_content"] == "def foobar"

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
        assert data["effective_ci"] is True

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

    def test_schema_requires_query_mode(self):
        schema = FindInFiles._PARAM_SCHEMA
        assert "query_mode" in schema["required"]
        assert schema["properties"]["query_mode"]["enum"] == [
            "text",
            "python-re",
            "python-re-dotall",
        ]
        assert "exclude_dirs" in schema["properties"]
        assert schema.get("additionalProperties") is False

    def test_text_sets_pattern_mode_like(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="hello",
            query_mode="text",
        )
        assert data["pattern_mode"] == "like"
        assert data["case_insensitive_arg"] is False

    def test_regexp_or_simple_mode(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("foo\n", encoding="utf-8")
        data = _call(
            with_local_ctx(FindInFiles),
            path=str(f),
            pattern="foo|bar",
            query_mode="python-re",
        )
        assert data["pattern_mode"] == "or_simple"
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
        assert "something" in m["line_content"]
        assert "happened" in m["line_content"]

    def test_python_re_inline_s_still_spans_lines(self, tmp_path):
        """Even without query_mode=dotall, inline (?s) enables DOTALL in the pattern."""
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
