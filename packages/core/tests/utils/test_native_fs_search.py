"""Нативный grep-рецепт: имена файлов в выводе, старт-симлинк, фильтр до обрезки."""

from gc_core.utils.native_fs_search import (
    _build_find_prune_argv,
    arun_native_grep,
)
from gc_core.utils.pattern_native_gate import NativeGrepPlan

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

    def exec(self, cmd, timeout=None, env=None, **kwargs):
        self.commands.append(cmd)
        self.last_capture_max_chars = kwargs.get("capture_max_chars")
        return self._res

    def exec_argv(self, argv, timeout=None, env=None, **kwargs):
        self.commands.append(" ".join(argv))
        self.last_capture_max_chars = kwargs.get("capture_max_chars")
        return self._res

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    def isabs(self, p):
        return p.startswith("/")


async def _run(backend, *, max_matches=10, allow_path=None):
    return await arun_native_grep(
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


async def test_grep_recipe_asks_for_file_names():
    """Без -H на порции из одного файла grep не печатает путь, и разбор врёт."""
    backend = _Backend()
    await _run(backend)
    assert "-H" in backend.commands[0].split()


async def test_native_grep_captures_at_walk_ceiling_not_tool_60k():
    """Постпроцесс внутри тула: не 60k трейса, а потолок walk (~50M)."""
    from gc_core.backends.protocol import WALK_OUTPUT_MAX_CHARS

    backend = _Backend(stdout="/work/a.py:1:KEY\n")
    await _run(backend)
    assert backend.last_capture_max_chars == WALK_OUTPUT_MAX_CHARS


async def test_single_file_recipe_asks_for_file_names():
    backend = _Backend()
    await arun_native_grep(
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


async def test_allow_path_filters_before_limit_truncation():
    """Запрещённые совпадения не должны съедать лимит: иначе разрешённые теряются."""
    stdout = "".join(f"/work/.env:{i}:SECRET_{i}\n" for i in range(1, 21))
    stdout += "/work/app.py:1:API_KEY = 1\n"
    backend = _Backend(stdout=stdout)

    res = await _run(
        backend, max_matches=5, allow_path=lambda p: not p.endswith(".env")
    )

    assert [m["file"] for m in res.matches] == ["/work/app.py"]
    assert res.truncated is False


async def test_allow_path_none_keeps_previous_behaviour():
    stdout = "".join(f"/work/a.txt:{i}:KEY_{i}\n" for i in range(1, 21))
    backend = _Backend(stdout=stdout)

    res = await _run(backend, max_matches=5)

    assert len(res.matches) == 5
    assert res.truncated is True


async def test_allow_path_truncates_allowed_matches_to_limit():
    stdout = "".join(f"/work/.env:{i}:SECRET_{i}\n" for i in range(1, 11))
    stdout += "".join(f"/work/app.py:{i}:KEY_{i}\n" for i in range(1, 11))
    backend = _Backend(stdout=stdout)

    res = await _run(
        backend, max_matches=4, allow_path=lambda p: not p.endswith(".env")
    )

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


async def test_absoluteness_of_grep_paths_is_decided_by_the_backend():
    backend = _AtSyntaxBackend(stdout="@root/app.py:1:API_KEY = 1\n")

    res = await arun_native_grep(
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

from gc_core.utils.native_fs_search import native_grep_refusal


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


def test_call_time_exclude_dirs_ride_the_native_recipe_when_find_is_there():
    assert _refusal(_ready(), call_exclude_dirs=["/work/vendor"]) is None


def test_call_time_exclude_dirs_need_find_for_the_path_prune():
    b = _ready()
    b.has_find = False
    assert _refusal(b, call_exclude_dirs=["/work/vendor"]) == "no_find"


def test_refusal_names_a_non_native_pattern():
    assert _refusal(_ready(), plan=None) == "pattern_not_native"


def test_no_refusal_when_the_recipe_is_available():
    assert _refusal(_ready()) is None


async def test_timeout_is_a_named_failure_not_none():
    b = _ready()
    b._res.timed_out = True
    res = await _run(b)
    assert res.failure == "native_timeout"
    assert res.matches == []


async def test_exec_exception_is_a_named_failure():
    class _Boom(_Backend):
        def exec(self, cmd, timeout=None, env=None, **kwargs):
            raise RuntimeError("no shell")

    assert (await _run(_Boom())).failure == "native_error"


async def test_unparseable_output_is_a_named_failure():
    assert (
        await _run(_Backend(stdout="garbage without separators\n"))
    ).failure == "native_error"


async def test_successful_run_has_no_failure():
    res = await _run(_Backend(stdout="/work/a.py:1:KEY\n"))
    assert res.failure is None
    assert [m["file"] for m in res.matches] == ["/work/a.py"]


# --- call-time exclude_dirs on the native recipe: find -path … -prune -------------


def test_prune_argv_excludes_call_time_paths():
    argv = _build_find_prune_argv(
        "/work",
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        exclude_paths=["/work/vendor"],
    )
    assert argv[argv.index("-path") + 1] == "/work/vendor"
    assert argv[argv.index("-prune") - 1] == ")"


def test_prune_argv_escapes_glob_metachars_in_paths():
    argv = _build_find_prune_argv(
        "/work",
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        exclude_paths=["/work/a[1]*?"],
    )
    assert argv[argv.index("-path") + 1] == r"/work/a\[1\]\*\?"


async def test_run_native_grep_prunes_call_time_paths():
    backend = _Backend()
    await arun_native_grep(
        backend,
        "/work",
        PLAN,
        exclude_dir_names=[".git"],
        exclude_nested_dot_dirs=True,
        file_pattern=None,
        max_matches=10,
        report_path=lambda p: p,
        is_dir_target=True,
        exclude_paths=["/work/vendor"],
    )
    assert "-path /work/vendor" in backend.commands[0]


def _needles(required=None, or_group=None):
    return Needles(
        required_literal=required, or_group=or_group, effective_ci=False
    )


async def _prefilter(backend, **overrides):
    kwargs = dict(
        terms=["KEY"],
        case_insensitive=False,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=True,
        file_pattern=None,
        exclude_paths=[],
    )
    kwargs.update(overrides)
    return await anative_prefilter_candidates(backend, "/work", **kwargs)


# --- needle prefilter in the sandbox: grep -l -F before any download ------------

from gc_core.utils.find_in_files_scan_mode import Needles
from gc_core.utils.native_fs_search import (
    anative_prefilter_candidates,
    build_prefilter_command,
    prefilter_terms,
)


def test_prefilter_terms_prefer_the_required_literal():
    assert prefilter_terms(
        _needles("KEY", ("1", "2")), case_insensitive=False
    ) == ["KEY"]


def test_prefilter_terms_use_the_or_group_when_nothing_is_required():
    assert prefilter_terms(
        _needles(None, ("bar", "baz")), case_insensitive=False
    ) == ["bar", "baz"]


def test_prefilter_terms_none_without_needles():
    assert prefilter_terms(_needles(), case_insensitive=False) is None


def test_prefilter_terms_refuse_non_ascii_when_case_insensitive():
    """grep -i сворачивает не-ASCII по локали контейнера — предфильтр был бы unsound."""
    assert prefilter_terms(_needles("ключ"), case_insensitive=True) is None
    assert prefilter_terms(_needles("ключ"), case_insensitive=False) == [
        "ключ"
    ]


def test_prefilter_terms_refuse_control_characters():
    assert prefilter_terms(_needles("a\nb"), case_insensitive=False) is None


def test_prefilter_command_lists_files_by_fixed_strings():
    cmd = build_prefilter_command(
        "/work",
        terms=["KEY"],
        case_insensitive=True,
        exclude_dir_names=[".git"],
        exclude_nested_dot_dirs=True,
        file_pattern=None,
        exclude_paths=[],
    )
    assert cmd.startswith("find -H /work -mindepth 1")
    assert (
        "| xargs -0 -r env " in cmd
    )  # env внутрь пайплайна: Harbor даёт его только первой команде
    assert (
        "grep -l --null -F -i" in cmd
    )  # --null: у BSD grep -Z значит «распаковать»
    assert "'(' -type f -o -type l ')'" in cmd
    assert "-e KEY" in cmd
    assert "--binary-files=without-match" not in cmd


def test_prefilter_command_keeps_utf16_files_in_play():
    """Python-путь читает UTF-16 с BOM; байты ASCII-needle grep там не найдёт —
    BOM-паттерны оставляют такие файлы в кандидатах (over-inclusive, но sound)."""
    cmd = build_prefilter_command(
        "/work",
        terms=["KEY"],
        case_insensitive=False,
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        exclude_paths=[],
    )
    assert r"$(printf '\377\376')" in cmd
    assert r"$(printf '\376\377')" in cmd


async def test_prefilter_candidates_are_absolute_paths_under_byte_semantics():
    class _EnvBackend(_Backend):
        def exec(self, cmd, timeout=None, env=None, **kwargs):
            self.env = env
            return super().exec(cmd, timeout, env, **kwargs)

    backend = _EnvBackend(stdout="/work/a.py\nsub/b.py\n")
    assert await _prefilter(backend) == (
        ["/work/a.py", "/work/sub/b.py"],
        None,
    )
    assert backend.env["LC_ALL"] == "C"


async def test_prefilter_candidates_no_match_is_an_empty_list():
    assert await _prefilter(_Backend(rc=1)) == ([], None)
    assert await _prefilter(_Backend(rc=123)) == ([], None)


async def test_prefilter_candidates_failure_is_named():
    class _Boom(_Backend):
        def exec(self, cmd, timeout=None, env=None, **kwargs):
            raise RuntimeError("no shell")

    assert await _prefilter(_Boom()) == (None, "failed")
    assert await _prefilter(_Backend(rc=2)) == (None, "failed")


async def test_prefilter_candidates_capped_output_is_named():
    """Обрезанный список — неполный, значит небезопасный: лучше обычный обход."""
    backend = _Backend(stdout="/work/a.py\n")
    backend._res.stdout_hit_capture_cap = True
    assert await _prefilter(backend) == (None, "capped")


# --- --null: имя файла с двоеточием больше не ломает разбор ----------------------


async def test_production_grep_terminates_file_names_with_nul():
    """GNU и BSD grep понимают --null (короткий -Z у BSD значит «распаковать как zgrep»)."""
    backend = _Backend(stdout="/work/a.py\x001:KEY\n")
    await _run(backend)
    assert "--null" in backend.commands[0].split()


def test_null_and_devices_skip_are_always_on():
    """Инвариант «пробуем тем же, чем выдаём»: оба флага в билдере безусловно —
    пробы capabilities зовут его же, busybox без --null честно провалит пробу.
    --devices=skip: симлинк на FIFO без писателя вешал grep до таймаута."""
    from gc_core.utils.native_fs_search import build_grep_argv_flags

    flags = build_grep_argv_flags()
    assert "--null" in flags
    assert "--devices=skip" in flags


async def test_nul_terminated_file_name_with_colon_is_parsed():
    res = await _run(
        _Backend(
            stdout="/work/app:2026-08-31.log\x001:x needle y\n/work/plain.txt\x003:needle here\n"
        )
    )
    assert res.failure is None
    assert [
        (m["file"], m["line"], m["line_content"]) for m in res.matches
    ] == [
        ("/work/app:2026-08-31.log", 1, "x needle y"),
        ("/work/plain.txt", 3, "needle here"),
    ]


async def test_colon_form_is_still_accepted_for_greps_without_nul():
    res = await _run(_Backend(stdout="/work/a.py:1:KEY\n"))
    assert [(m["file"], m["line"]) for m in res.matches] == [("/work/a.py", 1)]


# --- LC_ALL=C для ASCII-паттерна: cp1251-файл не пропускается молча ---------------


class _EnvSpyBackend(_Backend):
    def exec(self, cmd, timeout=None, env=None, **kwargs):
        self.env = env
        return super().exec(cmd, timeout, env, **kwargs)

    def exec_argv(self, argv, timeout=None, env=None, **kwargs):
        self.env = env
        return super().exec_argv(argv, timeout, env, **kwargs)


async def test_ascii_pattern_runs_grep_with_byte_semantics():
    """В UTF-8-локали grep считает cp1251-файл бинарным и молча пропускает; LC_ALL=C ищет байты."""
    backend = _EnvSpyBackend(stdout="/work/a.py\x001:KEY\n")
    await _run(backend)
    assert backend.env["LC_ALL"] == "C"


async def test_non_ascii_pattern_keeps_the_locale():
    """Кириллический паттерн байтами не ищется — локаль остаётся, как была."""
    backend = _EnvSpyBackend(stdout="")
    await arun_native_grep(
        backend,
        "/work",
        NativeGrepPlan(fixed=True, pattern="ключ", case_insensitive=False),
        exclude_dir_names=[],
        exclude_nested_dot_dirs=False,
        file_pattern=None,
        max_matches=10,
        report_path=lambda p: p,
        is_dir_target=True,
    )
    assert "LC_ALL" not in backend.env


async def test_pipeline_carries_the_env_to_grep_not_only_find():
    """Harbor подставляет env префиксом к строке команды — в пайплайне он достаётся
    только find; для grep присваивания должны стоять внутри пайплайна."""
    backend = _Backend(stdout="/work/a.py\x001:KEY\n")
    await _run(backend)
    cmd = backend.commands[0]
    tail = cmd.split("| xargs -0 -r ", 1)[1]
    assert tail.startswith("env ")
    assert "LC_ALL=C" in tail  # PLAN — фикс-строка ASCII
    assert "GREP_OPTIONS=" in tail


async def test_ere_ascii_pattern_keeps_the_locale():
    """Под LC_ALL=C точка и классы в ERE матчат байт, не символ (caf.s ≠ cafés) —
    байтовая локаль только для фикс-строк."""
    backend = _EnvSpyBackend(stdout="")
    await arun_native_grep(
        backend,
        "/work",
        NativeGrepPlan(fixed=False, pattern="caf.s", case_insensitive=False),
        exclude_dir_names=[".git"],
        exclude_nested_dot_dirs=True,
        file_pattern=None,
        max_matches=10,
        report_path=lambda p: p,
        is_dir_target=True,
    )
    assert "LC_ALL" not in (backend.env or {})
    tail = backend.commands[0].split("| xargs -0 -r ", 1)[1]
    assert "LC_ALL=C" not in tail


async def test_prefilter_pipeline_carries_env_to_grep():
    backend = _Backend(stdout="")
    await _prefilter(backend)
    cmd = backend.commands[0]
    tail = cmd.split("| xargs -0 -r ", 1)[1]
    assert tail.startswith("env ")
    assert "LC_ALL=C" in tail
