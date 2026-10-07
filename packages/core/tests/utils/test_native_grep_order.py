"""arun_native_grep без потолка совпадений: отдаёт всё, что разобрал.

Порядок по релевантности и обрезка по лимиту — дело инструмента (тот же ключ,
что у Python-пути); транспорт не должен резать вывод grep до сортировки.
"""

from gc_core.utils.native_fs_search import arun_native_grep
from gc_core.utils.pattern_native_gate import NativeGrepPlan

PLAN = NativeGrepPlan(fixed=True, pattern="needle", case_insensitive=False)


class _Res:
    def __init__(self, stdout, *, capped=False):
        self.stdout = stdout
        self.stderr = ""
        self.return_code = 0
        self.stdout_hit_capture_cap = capped
        self.timed_out = False


class _Backend:
    def __init__(self, stdout, *, capped=False):
        self._res = _Res(stdout, capped=capped)

    def exec(self, cmd, timeout=None, env=None, **kwargs):
        return self._res

    def exec_argv(self, argv, timeout=None, env=None, **kwargs):
        return self._res

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    def isabs(self, p):
        return p.startswith("/")


OUT = "/work/zzz.py:1:needle\n/work/needle.py:1:needle\n/work/needle.py:2:needle again\n"


async def _run(stdout, *, max_matches, capped=False):
    return await arun_native_grep(
        _Backend(stdout, capped=capped),
        "/work",
        PLAN,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        max_matches=max_matches,
        report_path=lambda p: p,
        is_dir_target=True,
    )


async def test_without_a_cap_every_parsed_match_is_returned():
    res = await _run(OUT, max_matches=None)
    assert [(m["file"], m["line"]) for m in res.matches] == [
        ("/work/zzz.py", 1),
        ("/work/needle.py", 1),
        ("/work/needle.py", 2),
    ]
    assert res.truncated is False


async def test_without_a_cap_only_the_transport_ceiling_marks_truncation():
    res = await _run(OUT, max_matches=None, capped=True)
    assert len(res.matches) == 3
    assert res.truncated is True
    assert res.hit_capture_cap is True


async def test_with_a_cap_the_old_contract_still_holds():
    res = await _run(OUT, max_matches=1)
    assert [(m["file"], m["line"]) for m in res.matches] == [
        ("/work/zzz.py", 1)
    ]
    assert res.truncated is True
