"""Хелпер сетки ленты `gc_testkit.trace_grid` (спека P4 §3.1).

Строки собраны вручную по макетам P4 (`p4.py`): каждый макет проходит, на
каждое правило — одно нарушение с понятным текстом. Колонка `●` верхнего
уровня — 1 (отступ ленты макетов).
"""

import pytest
from gc_testkit.trace_grid import assert_trace_grid, trace_rows
from gc_tui.palette import DIM, ERR, INK, MUTED, OK, REFUSED, RUN_COLOR
from gc_tui.palette import SUB_INK
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

ORIGIN = 1


def _r(text: str, *marks: tuple[str, str]) -> Text:
    """Строка ленты: целиком `DIM`, отмеченные куски — своим цветом."""
    row = Text()
    row.append(text, DIM)
    at = 0
    for part, colour in marks:
        i = text.index(part, at)
        row.stylize(colour, i, i + len(part))
        at = i + len(part)
    return row


def _pad(x: int) -> str:
    return " " * (ORIGIN + x)


def _prose(text: str, x: int = 0) -> Text:
    return _r(f"{_pad(x)}● {text}", ("●", DIM), (text, INK))


def _head(colour: str, word: str, tail: str = "", x: int = 0) -> Text:
    return _r(f"{_pad(x)}● {word}{tail}", ("●", colour), (word, INK))


def _member(verb, obj, t=None, *, x=0, lead=False) -> Text:
    mark = "└ " if lead else "  "
    timer = f" · {t}" if t else ""
    return _r(f"{_pad(x + 2)}{mark}{verb}  {obj}{timer}", (obj, MUTED))


def _lead_ctl(text: str, x: int = 0) -> Text:
    return _r(f"{_pad(x + 2)}└ {text}", (text, MUTED))


def _ctl(text: str, x: int = 0) -> Text:
    return _r(f"{_pad(x + 4)}{text}", (text, MUTED))


def _cons(text: str, colour: str, x: int = 0) -> Text:
    return _r(f"{_pad(x + 4)}└ {text}", (text, colour))


def _out(text: str, *, lead: bool, x: int = 0) -> Text:
    return _r(f"{_pad(x + 2)}{'└ ' if lead else '  '}{text}")


def _diff(no: int, sign: str, code: str, col: int) -> Text:
    gutter = f"{no:>{col + 3}} {sign}"
    colour = OK if sign == "+" else ERR
    return _r(f"{_pad(0)}{gutter} {code}", (gutter.strip(), colour))


def _file(path: str, tail: str, *, lead: bool = False, verb: str = "") -> Text:
    mark = "└ " if lead else "  "
    head = f"{verb} " if verb else ""
    marks = [(path, INK)]
    if verb:
        marks.insert(0, (verb, SUB_INK))
    return _r(f"{_pad(2)}{mark}{head}{path}{tail}", *marks)


BLANK = Text("")

P4_03_EXPLORED = [
    _prose("Посмотрю, как клиент делает запросы и где живут ретраи."),
    BLANK,
    _head(OK, "Explored"),
    _lead_ctl("… ещё 4"),
    _member("Find", "tests/*client* · 2 файла", "1с"),
    _member("Search", "RETRY_ON in packages/ · 3 совпадения", "3с"),
    _member("Read", "tests/test_client.py · 88 строк", "1с"),
    BLANK,
    _prose("Ретраи берут коды из RETRY_ON — 503 там нет."),
]

# Свёрнутый вывод — хвост; фраза — над ним, в колонке вывода (решение
# 2026-10-26): раскрывают там, где скрытое.
P4_07_RAN = [
    _prose("Прогоню тесты ретраев."),
    BLANK,
    _head(OK, "Ran", '  uv run pytest tests -q -k "retry and 503" · 6с'),
    _lead_ctl("… ещё 10 строк"),
    _out("FAILED tests/test_client.py::test_retry_503", lead=False),
    _out("=========== 1 failed, 13 passed in 0.4s ===========", lead=False),
    _out("make: *** [test] Error 1", lead=False),
]

# Ненулевой код — первой строкой, фраза — под ним, над хвостом.
P4_07_RAN_FAILED = [
    _head(ERR, "Ran", "  make test · 6с"),
    _r(f"{_pad(2)}└ код выхода 2", ("код выхода 2", ERR)),
    _ctl("… ещё 10 строк"),
    _out("FAILED tests/test_client.py::test_retry_503", lead=False),
    _out("make: *** [test] Error 1", lead=False),
]

# Многострочная команда: фраза команды — под её продолжением.
P4_RAN_MULTILINE = [
    _head(OK, "Ran", "  python - <<'EOF' · 2с"),
    _r(f"{_pad(4)}import json", ("import json", SUB_INK)),
    _r(f"{_pad(4)}print(1)", ("print(1)", SUB_INK)),
    _ctl("+ ещё 2 строки"),
    _lead_ctl("… ещё 11 строк"),
    _out("line 12", lead=False),
    _out("line 13", lead=False),
    _out("line 14", lead=False),
]

P4_10_RAN_FILES = [
    _prose("Сгенерирую таблицу задержек и уберу старый модуль."),
    BLANK,
    _head(OK, "Ran", "  python scripts/gen_retry_table.py · 2с"),
    _out("записано client/retry_table.py: 5 кодов", lead=True),
    _file("client/retry_table.py", " (+18)          ❐", verb="Added"),
    _diff(1, "+", '"""Сгенерировано."""', 4),
    _diff(2, "+", "", 4),
    _diff(3, "+", "TABLE = {", 4),
    BLANK,
    _file("client/__init__.py", " (+1)             ❐", verb="Edited"),
    _diff(3, "+", "from client.retry_table import TABLE", 4),
    BLANK,
    _file("client/legacy_retry.py", "", verb="Deleted"),
    _ctl("+ ещё 14 строк"),
]

P4_14_EDITED_3 = [
    _prose("Подключу модуль в клиенте, конфиге и тесте."),
    BLANK,
    _head(OK, "Edited", " 3 файла (+19 −16)"),
    _file("client/http.py", " (+4 −2) · 1с          ❐", lead=True),
    _diff(2, "-", "from client.transport import send", 4),
    _diff(2, "+", "from client.retry import with_retry", 4),
    _diff(42, "-", "        resp = send(req)", 4),
    BLANK,
    _file("client/config.py", " (+1 −1) · 1с        ❐"),
    _diff(12, "-", "TIMEOUT_SEC = 10", 4),
    _diff(12, "+", "TIMEOUT_SEC = 30", 4),
    BLANK,
    _file("tests/test_client.py", " (+14 −13) · 1с    ❐"),
    _diff(18, "-", "def test_no_retry_on_503(client):", 4),
    _ctl("+ ещё 25 строк"),
]

P4_18_AGENT = [
    _prose("Поручу проверку клиента субагенту."),
    BLANK,
    _head(OK, "Agent", "  Worker · 1м 12с"),
    _lead_ctl("Проверь, где клиент берёт таймаут, и прогони"),
    _r(f"{_pad(4)}тесты пакета…"),
    _r(f"{_pad(2)}… ещё 1", ("… ещё 1", MUTED)),
    _head(OK, "Explored", x=2),
    _lead_ctl("… ещё 1", x=2),
    _member("Read", "client/config.py · 40 строк", "1с", x=2),
    _member("Search", "timeout in client/ · 4 совпадения", "1с", x=2),
    _member("Read", "client/retry_policy.py · 12 строк", "1с", x=2),
    _head(OK, "Ran", "  uv run pytest tests -q · 11с", x=2),
    _out("42 passed in 3.10s", lead=True, x=2),
    _prose("Клиент читает таймаут из конфига, тесты проходят.", x=2),
    _head(OK, "Finished", "  full_solution", x=2),
    BLANK,
    _prose("Субагент подтвердил: таймаут и ретраи работают."),
]

# Раскрытое окно `Agent`: маркера нет, `− свернуть` — последней строкой
# блока `Agent`, в колонке маркера `… ещё N` (c + 2, без `└`).
P4_18_AGENT_EXPANDED = [
    _head(OK, "Agent", "  worker · «проверка клиента» · 1м 12с"),
    _prose("Смотрю, где клиент берёт таймаут.", x=2),
    _head(OK, "Ran", "  uv run pytest tests -q · 11с", x=2),
    _out("42 passed in 3.10s", lead=True, x=2),
    _prose("Клиент читает таймаут из конфига, тесты проходят.", x=2),
    _ctl("− свернуть", -2),
    BLANK,
    _prose("Субагент подтвердил: таймаут и ретраи работают."),
]

# Ханки правки не подряд — между ними разделитель `⋮` под номерами строк.
P4_14_HUNK_GAP = [
    _head(OK, "Edited", " 2 файла (+3 −1)"),
    _file("client/http.py", " (+2 −1) · 1с          ❐", lead=True),
    _diff(2, "-", "from client.transport import send", 4),
    _diff(2, "+", "from client.retry import with_retry", 4),
    _r(f"{_pad(0)}{'⋮':>{4 + 3}}"),
    _diff(43, "+", "        resp = with_retry(send, req,", 4),
    BLANK,
    _file("client/config.py", " (+1) · 1с        ❐"),
    _diff(12, "+", "TIMEOUT_SEC = 30", 4),
]

# Раскрытый дифф — с контекстом; пустая строка контекста — один номер.
P4_14_OPEN_CONTEXT = [
    _head(OK, "Edited", " 1 файл (+1 −1)"),
    _file("client/config.py", " (+1 −1) · 1с        ❐", lead=True),
    _r(f"{_pad(0)}{10:>{4 + 3}}   RETRIES = 3"),
    _r(f"{_pad(0)}{11:>{4 + 3}}"),
    _diff(12, "-", "TIMEOUT_SEC = 10", 4),
    _diff(12, "+", "TIMEOUT_SEC = 30", 4),
    _ctl("− свернуть"),
]

# Фраза — под тем, что раскрывает (решение 2026-10-26): у каждого
# обрезанного файла своя, под его диффом; у файла целиком — нет.
P4_EDITED_PER_FILE = [
    _head(OK, "Edited", " 3 файла (+12 −0)"),
    _file("a.py", " (+5 −0) · 1с          ❐", lead=True),
    _diff(1, "+", "a = 1", 4),
    _diff(2, "+", "b = 2", 4),
    _ctl("+ ещё 3 строки"),
    BLANK,
    _file("b.py", " (+2 −0) · 1с          ❐"),
    _diff(1, "+", "c = 3", 4),
    _diff(2, "+", "d = 4", 4),
    BLANK,
    _file("c.py", " (+5 −0) · 1с          ❐"),
    _diff(1, "+", "e = 5", 4),
    _ctl("− свернуть"),
]

# `Ran` с выводом и файлами: фраза вывода — над хвостом, файлы — ниже.
P4_RAN_OUTPUT_AND_FILES = [
    _head(OK, "Ran", "  python gen.py · 2с"),
    _lead_ctl("… ещё 4 строки"),
    _out("записано 5 кодов", lead=False),
    _out("готово", lead=False),
    _file("client/retry_table.py", " (+18)          ❐", verb="Added"),
    _diff(1, "+", '"""Сгенерировано."""', 4),
    _ctl("+ ещё 17 строк"),
    BLANK,
    _file("client/legacy_retry.py", "", verb="Deleted"),
]

P4_19_FAILURES = [
    _prose("Посмотрю конфиг клиента и настройки окружения."),
    BLANK,
    _r(
        " ● Explored · 1 ошибка · 1 отказ",
        ("●", OK),
        ("Explored", INK),
        (" · 1 ошибка", ERR),
        (" · 1 отказ", REFUSED),
    ),
    _lead_ctl("… ещё 3"),
    _member("Read", "client/http.py · 120 строк", "1с"),
    _member("Search", "RETRY_ON in packages/ · 3 совпадения", "1с"),
    _member("Find", "tests/*client* · 2 файла", "1с"),
    BLANK,
    _prose("Настройки окружения закрыты — беру значения из конфига."),
]

P4_22_EDIT_IN_GROUP = [
    _prose("Подключу модуль кодов в клиенте, конфиге и тесте."),
    BLANK,
    _head(OK, "Edited", " 3 файла (+5 −3) · 1 ошибка"),
    _file("client/http.py", " (+4 −2) · 1с          ❐", lead=True),
    _diff(2, "-", "from client.transport import send", 4),
    _diff(43, "+", "        resp = with_retry(send, req,", 4),
    BLANK,
    _file("client/config.py", " (+1 −1) · 1с        ❐"),
    _diff(12, "+", "TIMEOUT_SEC = 30", 4),
    BLANK,
    _file("tests/test_client.py", " · 1с"),
    _cons("не найден фрагмент", ERR),
    BLANK,
    _prose("В тесте фрагмент изменился — перечитаю файл и повторю."),
]


# Следствие члена — под началом его текста, и на уровне субагента: там
# сетка сдвинута на +2 вместе с глифом.
P4_NESTED_MEMBER_REASON = [
    _head(OK, "Agent", "  worker · 1с"),
    _lead_ctl("проверка клиента"),
    _head(ERR, "Explored", x=2),
    _member("Read", "client/legacy.py", lead=True, x=2),
    _cons("не найден", ERR, x=2),
]


@pytest.mark.parametrize(
    "rows",
    [
        P4_03_EXPLORED,
        P4_07_RAN,
        P4_07_RAN_FAILED,
        P4_RAN_MULTILINE,
        P4_10_RAN_FILES,
        P4_14_EDITED_3,
        P4_14_HUNK_GAP,
        P4_14_OPEN_CONTEXT,
        P4_EDITED_PER_FILE,
        P4_RAN_OUTPUT_AND_FILES,
        P4_18_AGENT,
        P4_18_AGENT_EXPANDED,
        P4_19_FAILURES,
        P4_22_EDIT_IN_GROUP,
        P4_NESTED_MEMBER_REASON,
    ],
    ids=[
        "p4-03",
        "p4-07",
        "p4-07-failed",
        "ran-multiline",
        "p4-10",
        "p4-14",
        "p4-14-hunk-gap",
        "p4-14-open-context",
        "edited-per-file",
        "ran-output-and-files",
        "p4-18",
        "p4-18-expanded",
        "p4-19",
        "p4-22",
        "nested-member-reason",
    ],
)
def test_mockup_passes(rows):
    assert assert_trace_grid(rows, origin=ORIGIN) is None


_EXPLORED_LEAD = [
    _head(OK, "Explored"),
    _member("Read", "a.py", "1с", lead=True),
]

VIOLATIONS = {
    "glyph-on-member": (
        [*_EXPLORED_LEAD, _r("     ● Read  b.py", ("●", OK))],
        "строка 3: глиф в колонке 4",
    ),
    "member-branch-without-member": (
        [_head(OK, "Explored"), _r("     └ Read  a.py · 1с")],
        "строка 2: следствие не под текстом своего члена",
    ),
    "member-reason-in-col-6": (
        [*_EXPLORED_LEAD, _r("       └ не найден", ("не найден", ERR))],
        "строка 3: `└` в колонке 6",
    ),
    "control-in-col-6": (
        [*_EXPLORED_LEAD, _r("       + ещё 3 строки", ("+ ещё", MUTED))],
        "строка 3: управление в колонке 6, ждали 4",
    ),
    "diff-in-col-3": (
        [_head(OK, "Edited", " a.py (+1)"), _diff(1, "+", "x = 1", 3)],
        "строка 2: дифф в колонке 3, ждали 2/4",
    ),
    "live-word-green-glyph": (
        [_head(OK, "Exploring")],
        "строка 1: живой глагол без янтарного глифа",
    ),
    "past-word-amber-glyph": (
        [_head(RUN_COLOR, "Explored")],
        "строка 1: прошедшее время с янтарным глифом",
    ),
    "member-text-in-error-colour": (
        [*_EXPLORED_LEAD, _r("     Read  b.py", ("Read  b.py", ERR))],
        "строка 3: текст члена цветом сбоя",
    ),
    "member-reason-muted": (
        [*_EXPLORED_LEAD, _cons("не найден", MUTED)],
        "строка 3: причина члена не цветом исхода",
    ),
    "timer-on-refusal": (
        [*_EXPLORED_LEAD, _cons("отказ: защита путей", REFUSED)],
        "строка 3: у неисполненного вызова есть таймер",
    ),
    "nested-glyph-in-c-plus-4": (
        [
            _head(OK, "Agent", "  worker · 1с"),
            _lead_ctl("задача"),
            _r("     ● Explored", ("●", OK)),
        ],
        "строка 3: глиф в колонке 4",
    ),
    "agent-marker-in-c-plus-4": (
        [
            _head(OK, "Agent", "  Worker · 1с"),
            _lead_ctl("задача"),
            _ctl("… ещё 1"),
        ],
        "строка 3: маркер окна `Agent` в колонке 4, ждали 2",
    ),
    "agent-marker-with-branch": (
        [
            _head(OK, "Agent", "  Worker · 1с"),
            _lead_ctl("задача"),
            _lead_ctl("… ещё 1"),
        ],
        "строка 3: маркер окна `Agent` с `└`",
    ),
    "agent-collapse-in-c-plus-4": (
        [
            _head(OK, "Agent", "  worker · 1с"),
            _prose("Смотрю.", x=2),
            _ctl("− свернуть"),
        ],
        "строка 3: управление в колонке 4, ждали 6",
    ),
    "agent-expand-phrase-in-c-plus-4": (
        [
            _head(OK, "Agent", "  worker · 1с"),
            _prose("Смотрю.", x=2),
            _ctl("+ ещё 3 строки"),
        ],
        "строка 3: управление в колонке 4, ждали 6",
    ),
    "expand-phrase-inside-diff": (
        [
            _head(OK, "Edited", " 2 файла (+9 −0)"),
            _file("a.py", " (+5 −0)", lead=True),
            _diff(1, "+", "a = 1", 4),
            _ctl("+ ещё 4 строки"),
            _diff(2, "+", "b = 2", 4),
        ],
        "строка 4: фраза раскрытия посреди диффа",
    ),
    "expand-phrase-under-control": (
        [
            _head(OK, "Edited", " 4 файла (+9 −0)"),
            _lead_ctl("… ещё 1"),
            _ctl("+ ещё 4 строки"),
        ],
        "строка 3: фраза раскрытия не под тем, что раскрывает",
    ),
    "single-ran-reason-not-glyph-colour": (
        [
            _head(ERR, "Ran", "  make docs · 1с"),
            _cons("код выхода 2", DIM, -2),
        ],
        "строка 2: причина одиночного вызова не цветом исхода",
    ),
}


@pytest.mark.parametrize("name", list(VIOLATIONS))
def test_violation_fails_with_clear_text(name):
    rows, message = VIOLATIONS[name]
    with pytest.raises(AssertionError) as caught:
        assert_trace_grid(rows, origin=ORIGIN)
    assert str(caught.value).startswith(message)


def test_failure_names_the_row_text():
    rows = [_head(OK, "Exploring")]
    with pytest.raises(AssertionError) as caught:
        assert_trace_grid(rows, origin=ORIGIN)
    assert str(caught.value).endswith(repr(" ● Exploring"))


# ---- строки отрендеренной ленты -----------------------------------------


class _Keep(Static):
    pass


class _Drop(Static):
    pass


class _TraceApp(App):
    def __init__(self, *rows: Static) -> None:
        super().__init__()
        self._trace_rows = rows

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="trace"):
            yield from self._trace_rows


def _colour_at(row: Text, i: int) -> str:
    colours = [
        Style.parse(s.style) if isinstance(s.style, str) else s.style
        for s in row.spans
        if s.start <= i < s.end
    ]
    return colours[-1].color.get_truecolor().hex.lower()


async def test_trace_rows_keep_text_and_style():
    line = Text()
    line.append("● ", OK)
    line.append("Explored", INK)
    app = _TraceApp(_Keep(line))
    async with app.run_test(size=(40, 4)):
        rows = trace_rows(app)

    assert rows[0].plain.startswith("● Explored")
    assert _colour_at(rows[0], 0) == OK.lower()
    assert _colour_at(rows[0], 2) == INK.lower()


async def test_trace_rows_exclude_drops_rows_of_that_type():
    app = _TraceApp(_Keep("первая"), _Drop("итог хода"), _Keep("вторая"))
    async with app.run_test(size=(40, 5)):
        rows = trace_rows(app, exclude=(_Drop,))

    plains = [r.plain.rstrip() for r in rows]
    assert plains[:2] == ["первая", "вторая"]
    assert all("итог" not in p for p in plains)


async def test_trace_rows_without_scrollbar_columns():
    """Строка — область содержимого ленты: колонок полосы прокрутки в
    ней нет."""
    app = _TraceApp(*[_Keep(f"строка {i}") for i in range(20)])
    async with app.run_test(size=(40, 5)):
        trace = app.screen.query_one("#trace")
        assert trace.scrollbar_size_vertical > 0
        rows = trace_rows(app)
        width = trace.scrollable_content_region.right - trace.region.x
        height = trace.region.height

    assert {row.cell_len for row in rows} == {width}
    assert width == 38
    assert len(rows) == height


def test_ran_error_top_phrase_not_a_reason():
    """Упавшая без кода выхода команда с длинным выводом: первая `└` —
    фраза над хвостом, не причина; цвет причины с неё не спрашивается."""
    rows = [
        _head(ERR, "Ran", "  make · 1с"),
        _lead_ctl("… ещё 4 строки"),
        _out("x", lead=False),
    ]
    assert_trace_grid(rows, origin=ORIGIN)


def test_executed_failure_may_have_no_timer():
    """Упавший исполненный член без таймера законен: вызов быстрее секунды
    или время вызовов неизвестно (§3.4)."""
    rows = [
        _head(ERR, "Explored"),
        _member("Read", "a.py", lead=True),
        _cons("не найден", ERR),
    ]
    assert_trace_grid(rows, origin=ORIGIN)
