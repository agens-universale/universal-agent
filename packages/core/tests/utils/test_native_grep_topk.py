"""Потоковый top-K в arun_native_grep.

Ранжирование смотрит на все строки вывода grep, но в памяти живут только K лучших
(куча размером в лимит) плюс счётчик строк и множество файлов для карты «где
густо». Ответ бит в бит совпадает со «разобрать всё → отсортировать → срезать».
"""

from gc_core.utils.native_fs_search import arun_native_grep
from gc_core.utils.pattern_native_gate import NativeGrepPlan

PLAN = NativeGrepPlan(fixed=True, pattern="needle", case_insensitive=False)


class _Res:
    def __init__(self, stdout, *, capped=False):
        self.stdout = stdout
        self.stderr = ""
        self.return_code = 0
        self.timed_out = False
        self.stdout_hit_capture_cap = capped


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


def _key(path):
    """Как у тула: файл с иглой в имени — выше, потом по пути."""
    name = path.rsplit("/", 1)[-1]
    return (0 if "needle" in name else 1, path)


def _stdout(files=20, lines_per_file=10):
    out = []
    for f in range(files):
        # файл с иглой в имени стоит в конце дискового порядка — он должен победить
        name = f"/work/pkg/needle_{f}.py" if f == files - 1 else f"/work/pkg/m{f:02d}.py"
        for n in range(1, lines_per_file + 1):
            out.append(f"{name}\x00{n}:needle {f} {n}\n")
    return "".join(out)


async def _run(backend, *, report_path=None, allow_path=None, **kw):
    return await arun_native_grep(
        backend,
        "/work",
        PLAN,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        max_matches=None,
        report_path=report_path or (lambda p: p),
        is_dir_target=True,
        allow_path=allow_path,
        **kw,
    )


def _pairs(matches):
    return [(m["file"], m["line"]) for m in matches]


async def test_top_k_equals_sort_then_cut():
    stdout = _stdout()
    everything = await _run(_Backend(stdout))
    expected = sorted(everything.matches, key=lambda m: _key(m["file"]))[:5]

    top = await _run(_Backend(stdout), rank_key=_key, top_k=5)

    assert _pairs(top.matches) == _pairs(expected)
    assert top.matches[0]["file"].endswith("needle_19.py")  # победил файл из хвоста диска
    assert top.total_matches == 200
    assert top.files_seen == {m["file"] for m in everything.matches}
    assert top.failure is None and top.truncated is False


async def test_top_k_builds_records_only_for_the_winners():
    calls = []

    def report(p):
        calls.append(p)
        return p

    top = await _run(_Backend(_stdout()), report_path=report, rank_key=_key, top_k=5)

    assert len(top.matches) == 5
    assert len(calls) == 5  # словари строятся только для победителей, не для 200 строк


async def test_top_k_respects_allow_path_before_ranking_and_counting():
    denied = lambda p: "m00" not in p

    top = await _run(_Backend(_stdout()), allow_path=denied, rank_key=_key, top_k=50)

    assert all("m00" not in m["file"] for m in top.matches)
    assert top.total_matches == 190
    assert not any("m00" in f for f in top.files_seen)


async def test_top_k_torn_tail_is_flagged_when_it_wins():
    stdout = "/work/a.py\x001:needle a\n/work/needle_z.py\x007:needle cut mid-li"
    top = await _run(_Backend(stdout, capped=True), rank_key=_key, top_k=5)

    assert top.hit_capture_cap is True and top.truncated is True
    assert top.matches[0]["file"] == "/work/needle_z.py"
    assert top.matches[0].get("line_content_truncated") is True
    assert "line_content_truncated" not in top.matches[1]


async def test_top_k_unparseable_middle_line_is_still_a_failure():
    stdout = "/work/a.py\x001:needle\nsome foreign line\n/work/b.py\x002:needle\n"
    top = await _run(_Backend(stdout), rank_key=_key, top_k=5)
    assert top.failure == "native_error"
    assert top.matches == []
