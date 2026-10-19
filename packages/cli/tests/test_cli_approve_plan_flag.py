"""`gc-cli --approve-plan`: одобрить предложенный план сессии без терминала."""

import sys

import pytest

# `import cli` парсит sys.argv при импорте (--query обязателен).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import arg_parser, cli  # noqa: E402
from gc_cli.plan_approval import thread_has_proposed_plan  # noqa: E402
from gc_core.plan_store import new_plan_block  # noqa: E402
from gc_core.thread_store import save_thread_meta  # noqa: E402
from gc_testkit import run_coro  # noqa: E402
from gc_testkit.settings import make_settings  # noqa: E402


def _parse(*argv):
    parsed, _ = arg_parser.parser.parse_known_args(list(argv))
    arg_parser._check_combinations(parsed)
    return parsed


def test_flag_takes_an_execution_mode_and_needs_a_session():
    assert _parse("-t", "s1", "--approve-plan", "Auto").approve_plan == "auto"
    parsed = _parse("-t", "s1", "--approve-plan", "edit", "-q", "кратко")
    assert (parsed.approve_plan, parsed.query) == ("edit", "кратко")
    with pytest.raises(SystemExit):
        _parse("--approve-plan", "edit")  # без -t
    with pytest.raises(SystemExit):
        _parse("-t", "s1", "--approve-plan", "plan")  # не режим исполнения


@pytest.mark.parametrize(
    "extra",
    [
        ["-n"],
        ["--permission-mode", "auto"],
        ["--image", "a.png"],
        ["-s", "review"],
        ["-T"],
    ],
)
def test_flag_refuses_options_it_would_ignore(extra):
    with pytest.raises(SystemExit):
        _parse("-t", "s1", "--approve-plan", "edit", *extra)


def _save_plan(threads, status):
    plan = {**new_plan_block("1. шаг"), "status": status}
    save_thread_meta(threads, "cli", "s1", {"shared": {"plan": plan}})


def test_only_a_proposed_plan_of_the_saved_session_counts(tmp_path):
    threads = str(tmp_path)
    assert thread_has_proposed_plan(threads, "s1") is False  # сессии нет
    _save_plan(threads, "proposed")
    assert thread_has_proposed_plan(threads, "s1") is True
    _save_plan(threads, "approved")
    assert thread_has_proposed_plan(threads, "s1") is False


def test_without_a_proposed_plan_the_run_ends_with_code_1(tmp_path, capsys):
    settings = make_settings({"paths.threads_data_path": str(tmp_path)})
    assert cli._approve_plan_exit(settings, None, "s1") is None
    assert cli._approve_plan_exit(settings, "edit", "s1") == 1
    assert "нет предложенного плана" in capsys.readouterr().err
    _save_plan(str(tmp_path), "proposed")
    assert cli._approve_plan_exit(settings, "edit", "s1") is None


class _Executor:
    def __init__(self):
        self.calls = []

    async def aexecute_accepted_plan(self, mode, thread_id, **kwargs):
        self.calls.append((mode, thread_id, kwargs))
        return "исполнено"

    async def arun(self, *args, **kwargs):
        raise AssertionError("обычный запрос не должен уходить")


@pytest.mark.parametrize("mode", ["edit", "auto", "manual"])
def test_flag_runs_the_approval_sequence_instead_of_the_query(mode):
    ex = _Executor()
    result = run_coro(
        cli._arun_requested_turn(
            ex,
            ["кратко"],
            approve_mode=mode,
            extra_text="кратко",
            thread_id="s1",
            start_new_thread=False,
            verbose=False,
            attachments=None,
            account_run=True,
        )
    )
    assert result == "исполнено"
    assert ex.calls == [
        (
            mode,
            "s1",
            {"extra_text": "кратко", "verbose": False, "account_run": True},
        )
    ]
