"""`gc-cli --inbox`: ход из входящего потока треда без своего запроса —
продолжение задачи swarm, когда песочница вышла, а в треде осталось
недоставленное сообщение."""

import sys

import pytest

# `import cli` парсит sys.argv при импорте (--query обязателен).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import arg_parser, cli  # noqa: E402


def _parse(*argv):
    parsed, _ = arg_parser.parser.parse_known_args(list(argv))
    arg_parser._check_combinations(parsed)
    return parsed


def test_inbox_turn_needs_a_thread():
    assert _parse("-t", "s1", "--inbox").inbox
    with pytest.raises(SystemExit):
        _parse("--inbox")


@pytest.mark.parametrize(
    "extra",
    [
        ["-q", "x"],
        ["-n"],
        ["--approve-plan", "edit"],
        ["--image", "a.png"],
        ["-s", "review"],
        ["-T"],
    ],
)
def test_inbox_turn_refuses_its_own_input(extra):
    with pytest.raises(SystemExit):
        _parse("-t", "s1", "--inbox", *extra)


def test_inbox_turn_has_no_user_messages():
    """Пустой список — ход из входящего потока (``AgentExecutor.arun``)."""
    assert cli._cli_user_parts(
        None, [], first_n=1, wrap_technical=False, forced_skill_id=None
    ) == ([], None)
