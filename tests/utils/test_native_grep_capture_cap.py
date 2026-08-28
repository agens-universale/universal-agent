"""Потолок захвата stdout режет вывод grep посреди строки.

Оборванный хвост всё равно называет настоящие файл и номер строки (у
минифицированного бандла это может быть единственное совпадение), поэтому он
остаётся в ответе с пометкой line_content_truncated, а не выбрасывается.
"""

from utils.native_fs_search import run_native_grep
from utils.pattern_native_gate import NativeGrepPlan

PLAN = NativeGrepPlan(fixed=True, pattern="KEY", case_insensitive=False)


class _Res:
    def __init__(self, stdout, *, capped):
        self.stdout = stdout
        self.stderr = ""
        self.return_code = 0
        self.stdout_hit_capture_cap = capped
        self.timed_out = False


class _Backend:
    def __init__(self, stdout, *, capped):
        self._res = _Res(stdout, capped=capped)

    def exec(self, cmd, timeout=None, env=None, **kwargs):
        return self._res

    def exec_argv(self, argv, timeout=None, env=None, **kwargs):
        return self._res

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    def isabs(self, p):
        return p.startswith("/")


def _run(stdout, *, capped):
    return run_native_grep(
        _Backend(stdout, capped=capped),
        "/work",
        PLAN,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        max_matches=100,
        report_path=lambda p: p,
        is_dir_target=True,
    )


def test_capped_output_keeps_the_cut_last_line_but_flags_it():
    res = _run("/work/a.py:1:KEY = 1\n/work/a.py:2:KEY = ", capped=True)
    assert [(m["line"], m["line_content"]) for m in res.matches] == [(1, "KEY = 1"), (2, "KEY = ")]
    assert "line_content_truncated" not in res.matches[0]
    assert res.matches[1]["line_content_truncated"] is True
    assert res.truncated is True
    assert res.hit_capture_cap is True


def test_capped_output_ending_on_a_newline_flags_nothing():
    res = _run("/work/a.py:1:KEY = 1\n/work/a.py:2:KEY = 2\n", capped=True)
    assert [m["line"] for m in res.matches] == [1, 2]
    assert all("line_content_truncated" not in m for m in res.matches)
    assert res.truncated is True


def test_uncapped_output_without_trailing_newline_is_complete():
    res = _run("/work/a.py:1:KEY = 1\n/work/a.py:2:KEY = 2", capped=False)
    assert [m["line"] for m in res.matches] == [1, 2]
    assert all("line_content_truncated" not in m for m in res.matches)
    assert res.truncated is False


def test_capped_output_drops_an_unparseable_tail_and_keeps_the_prefix():
    """Срез внутри file:line: — не провал всего буфера и не Python-download."""
    res = _run("/work/a.py:1:KEY = 1\n/work/a.py:2", capped=True)
    assert [(m["line"], m["line_content"]) for m in res.matches] == [(1, "KEY = 1")]
    assert "line_content_truncated" not in res.matches[0]
    assert res.failure is None
    assert res.hit_capture_cap is True
    assert res.truncated is True


def test_uncapped_unparseable_line_is_still_a_failure():
    res = _run("/work/a.py:1:KEY = 1\n/work/a.py:2", capped=False)
    assert res.failure == "native_error"
    assert res.matches == []


def test_capped_output_with_a_foreign_line_in_the_middle_is_still_a_failure():
    """Оборванный хвост — только последняя строка; чужая строка посреди потока — сбой и откат."""
    res = _run("/work/a.py:1:KEY = 1\nsome foreign line\n/work/b.py:2:KEY = 2\n", capped=True)
    assert res.failure == "native_error"
    assert res.matches == []
