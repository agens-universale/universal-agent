"""Префильтр по needle: исход не молчит — (paths, None) при успехе, (None, причина) при отказе."""

from gc_core.utils.native_fs_search import (
    PREFILTER_TIMEOUT_SEC,
    anative_prefilter_candidates,
)


class _Res:
    def __init__(self, stdout="", rc=0, *, timed_out=False, capped=False):
        self.stdout = stdout
        self.stderr = ""
        self.return_code = rc
        self.timed_out = timed_out
        self.stdout_hit_capture_cap = capped


class _Backend:
    def __init__(self, res=None, *, raise_exc=None):
        self._res = res or _Res()
        self._raise = raise_exc
        self.calls = []

    def exec(self, cmd, timeout=None, env=None, **kwargs):
        self.calls.append({"cmd": cmd, "timeout": timeout, **kwargs})
        if self._raise is not None:
            raise self._raise
        return self._res

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    def isabs(self, p):
        return p.startswith("/")


async def _run(backend):
    return await anative_prefilter_candidates(
        backend,
        "/work",
        terms=["KEY"],
        case_insensitive=False,
        exclude_dir_names=[".git"],
        exclude_nested_dot_dirs=True,
        file_pattern=None,
        exclude_paths=(),
    )


async def test_success_returns_paths_and_no_failure():
    backend = _Backend(_Res("/work/a.py\nb.py\n"))
    assert await _run(backend) == (["/work/a.py", "/work/b.py"], None)


async def test_timeout_is_named():
    assert await _run(_Backend(_Res(timed_out=True))) == (None, "timeout")


async def test_capped_listing_is_named():
    assert await _run(_Backend(_Res("/work/a.py\n", capped=True))) == (
        None,
        "capped",
    )


async def test_exec_error_and_bad_exit_are_named():
    assert await _run(_Backend(raise_exc=RuntimeError("boom"))) == (
        None,
        "failed",
    )
    assert await _run(_Backend(_Res(rc=2))) == (None, "failed")


async def test_prefilter_budget_is_one_minute():
    backend = _Backend(_Res())
    await _run(backend)
    assert PREFILTER_TIMEOUT_SEC == 60
    assert backend.calls[0]["timeout"] == PREFILTER_TIMEOUT_SEC


async def test_prefilter_captures_at_walk_ceiling_not_tool_60k():
    from gc_core.backends.protocol import WALK_OUTPUT_MAX_CHARS

    backend = _Backend(_Res())
    await _run(backend)
    assert backend.calls[0]["capture_max_chars"] == WALK_OUTPUT_MAX_CHARS
