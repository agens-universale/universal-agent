"""Нативный grep-рецепт: имена файлов в выводе, старт-симлинк, фильтр до обрезки."""

from utils.native_fs_search import _build_find_prune_argv, run_native_grep
from utils.pattern_native_gate import NativeGrepPlan

PLAN = NativeGrepPlan(fixed=True, pattern="KEY", case_insensitive=False)


class _Res:
    def __init__(self, stdout="", rc=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = rc
        self.stdout_hit_capture_cap = False
        self.timed_out = False


class _Backend:
    """Стаб бэкенда: запоминает команду, отдаёт заранее заданный stdout."""

    def __init__(self, stdout="", rc=0):
        self.commands = []
        self._res = _Res(stdout, rc)

    def exec(self, cmd, timeout=None, env=None):
        self.commands.append(cmd)
        return self._res

    def exec_argv(self, argv, timeout=None, env=None):
        self.commands.append(" ".join(argv))
        return self._res

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    def isabs(self, p):
        return p.startswith("/")


def _run(backend, *, max_matches=10, allow_path=None):
    return run_native_grep(
        backend,
        "/work",
        PLAN,
        exclude_dir_names=[".git"],
        exclude_nested_dot_dirs=True,
        file_pattern=None,
        max_matches=max_matches,
        report_path=lambda p: p,
        is_dir_target=True,
        allow_path=allow_path,
    )


def test_find_follows_symlinked_start_path():
    """find без -H не разворачивает стартовую ссылку: ноль путей при непустом дереве."""
    argv = _build_find_prune_argv(
        "/work",
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
    )
    assert argv[:3] == ["find", "-H", "/work"]


def test_grep_recipe_asks_for_file_names():
    """Без -H на порции из одного файла grep не печатает путь, и разбор врёт."""
    backend = _Backend()
    _run(backend)
    assert "-H" in backend.commands[0].split()


def test_single_file_recipe_asks_for_file_names():
    backend = _Backend()
    run_native_grep(
        backend,
        "/work/a.txt",
        PLAN,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        max_matches=10,
        report_path=lambda p: p,
        is_dir_target=False,
    )
    assert "-H" in backend.commands[0].split()


def test_allow_path_filters_before_limit_truncation():
    """Запрещённые совпадения не должны съедать лимит: иначе разрешённые теряются."""
    stdout = "".join(f"/work/.env:{i}:SECRET_{i}\n" for i in range(1, 21))
    stdout += "/work/app.py:1:API_KEY = 1\n"
    backend = _Backend(stdout=stdout)

    res = _run(backend, max_matches=5, allow_path=lambda p: not p.endswith(".env"))

    assert [m["file"] for m in res.matches] == ["/work/app.py"]
    assert res.truncated is False


def test_allow_path_none_keeps_previous_behaviour():
    stdout = "".join(f"/work/a.txt:{i}:KEY_{i}\n" for i in range(1, 21))
    backend = _Backend(stdout=stdout)

    res = _run(backend, max_matches=5)

    assert len(res.matches) == 5
    assert res.truncated is True


def test_allow_path_truncates_allowed_matches_to_limit():
    stdout = "".join(f"/work/.env:{i}:SECRET_{i}\n" for i in range(1, 11))
    stdout += "".join(f"/work/app.py:{i}:KEY_{i}\n" for i in range(1, 11))
    backend = _Backend(stdout=stdout)

    res = _run(backend, max_matches=4, allow_path=lambda p: not p.endswith(".env"))

    assert [m["file"] for m in res.matches] == ["/work/app.py"] * 4
    assert res.truncated is True


class _AtSyntaxBackend(_Backend):
    """Бэкенд с собственным синтаксисом путей: абсолютный начинается с '@'.

    Хостовый os.path.isabs на такой путь отвечает «относительный» на любой ОС и
    любой версии Python — поэтому страж ловит регрессию везде, а не только на
    Windows с CPython 3.13, где так же ведёт себя '/work/app.py'.
    """

    def isabs(self, p):
        return p.startswith("@")


def test_absoluteness_of_grep_paths_is_decided_by_the_backend():
    backend = _AtSyntaxBackend(stdout="@root/app.py:1:API_KEY = 1\n")

    res = run_native_grep(
        backend,
        "@root",
        PLAN,
        exclude_dir_names=[".git"],
        exclude_nested_dot_dirs=True,
        file_pattern=None,
        max_matches=10,
        report_path=lambda p: p,
        is_dir_target=True,
    )

    assert [m["file"] for m in res.matches] == ["@root/app.py"]


# --- причины отказа и виды сбоев: нативный путь называет, почему ответит Python ---

from utils.native_fs_search import native_grep_refusal


def _ready(stdout="", rc=0):
    b = _Backend(stdout, rc)
    b.has_grep = True
    b.has_find = True
    return b


def _refusal(backend, *, plan=PLAN, call_exclude_dirs=None):
    return native_grep_refusal(
        backend,
        plan=plan,
        exclude_dir_names=[".git"],
        call_exclude_dirs=call_exclude_dirs,
        file_pattern=None,
    )


def test_refusal_names_the_missing_grep():
    b = _Backend()
    b.has_grep = False
    assert _refusal(b) == "no_grep"


def test_refusal_names_call_time_exclude_dirs():
    assert _refusal(_ready(), call_exclude_dirs=["/work/vendor"]) == "exclude_dirs"


def test_refusal_names_a_non_native_pattern():
    assert _refusal(_ready(), plan=None) == "pattern_not_native"


def test_no_refusal_when_the_recipe_is_available():
    assert _refusal(_ready()) is None


def test_timeout_is_a_named_failure_not_none():
    b = _ready()
    b._res.timed_out = True
    res = _run(b)
    assert res.failure == "native_timeout"
    assert res.matches == []


def test_exec_exception_is_a_named_failure():
    class _Boom(_Backend):
        def exec(self, cmd, timeout=None, env=None):
            raise RuntimeError("no shell")

    assert _run(_Boom()).failure == "native_error"


def test_unparseable_output_is_a_named_failure():
    assert _run(_Backend(stdout="garbage without separators\n")).failure == "native_error"


def test_successful_run_has_no_failure():
    res = _run(_Backend(stdout="/work/a.py:1:KEY\n"))
    assert res.failure is None
    assert [m["file"] for m in res.matches] == ["/work/a.py"]
