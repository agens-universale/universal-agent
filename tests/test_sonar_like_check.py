"""Сетка на `scripts/sonar_like_check.py` — локальный аналог правил Sonar.

Чекер стоит в pre-commit и в `scripts/lint.sh`, то есть решает, пройдёт ли
коммит. Поэтому на каждое правило здесь пара «находка / не находка»: молчащий
чекер так же плох, как и шумящий, а без негативного примера ложное
срабатывание замечают только после того, как человек поставит `NOSONAR` на
живой код.

Отдельно закреплена калибровка S5843. Порог «больше 20» сам по себе ничего не
значит: важно, что наш счётчик даёт те же числа, что и Sonar. Эталон —
регулярка `<meta …>` из `simple_fetch_url.py` (коммит 880f9930), на которую
Sonar выписал ровно 24; вторая точка — регулярка ключа YAML из
`onboarding_config_writer.py`, которую он НЕ пометил, то есть у неё <= 20.

Скрипт лежит в `scripts/` и пакетом не является — грузим его по пути.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from gc_testkit.paths import REPO

_SCRIPT = REPO / "scripts" / "sonar_like_check.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("sonar_like_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _findings(
    tmp_path: Path, source: str, name: str = "sample.py"
) -> list[str]:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return checker.check_file(path)


def _keys(findings: list[str]) -> list[str]:
    return [f.split(": ", 1)[1].split(" ", 1)[0] for f in findings]


# --------------------------------------------------------------------------- #
# S5843 — калибровка по отчёту Sonar
# --------------------------------------------------------------------------- #
#: `<meta name=description content=…>` из simple_fetch_url.py на 880f9930.
CALIBRATION_REGEX = (
    r"""<meta\b(?=[^>]*\bname\s*=\s*(['"])description\1)"""
    r"""(?=[^>]*\bcontent\s*=\s*(['"])(.*?)\2)[^>]*>"""
)
#: Ключ YAML из onboarding_config_writer.py — Sonar её не пометил.
QUIET_REGEX = r"^(\s*)([A-Za-z0-9_]+)\s*:"


def test_s5843_calibration_meta_regex_is_24():
    assert checker.regex_complexity(CALIBRATION_REGEX) == 24


def test_s5843_calibration_yaml_key_regex_is_within_limit():
    assert checker.regex_complexity(QUIET_REGEX) <= checker.REGEX_MAX


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"abc", 0),  # одни литералы
        (r"\s+", 1),  # повтор на первом уровне
        (r"[a-z]+", 2),  # класс +1, повтор +1
        (r"(ab|cd)", 1),  # первая «|» стоит «уровень»
        (r"(ab|cd|ef)", 2),  # каждая следующая — +1
        (
            r"(a|b)",
            0,
        ),  # парсер схлопывает односимвольные ветви в класс — см. докстроку
        (r"(?=ab)", 1),  # lookahead
        (r"(a)\1", 1),  # обратная ссылка
        (r"(?:a+)+", 3),  # вложенный повтор: 1 + 2
    ],
)
def test_s5843_cost_model(pattern, expected):
    assert checker.regex_complexity(pattern) == expected


def test_s5843_reported_only_above_threshold(tmp_path):
    hit = _findings(
        tmp_path, f'import re\nre.search(r"""{CALIBRATION_REGEX}""", "")\n'
    )
    assert _keys(hit) == ["S5843"]
    assert "from 24 to the 20 allowed" in hit[0]
    quiet = _findings(
        tmp_path,
        f'import re\nre.match(r"{QUIET_REGEX}", "")\n',
        name="quiet.py",
    )
    assert quiet == []  # ASCII-диапазон нельзя заменять Unicode-классом \w


# --------------------------------------------------------------------------- #
# S6353 / S5857 — подсказки по регуляркам
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("pattern", "key"),
    [
        (r"[A-Za-z0-9_]+", "S6353"),
        (r"[0-9]{2}", "S6353"),
        (r"<b>.*?<", "S5857"),
    ],
)
def test_regex_hints_found(tmp_path, pattern, key):
    assert _keys(
        _findings(
            tmp_path,
            f'import re\nre.search(r"{pattern}", "", flags=re.ASCII)\n',
        )
    ) == [key]


@pytest.mark.parametrize(
    "pattern", [r"\w+", r"\d{2}", r"[^<]*<", r"[A-Za-z_]+", r"[0-9a-f]+"]
)
def test_regex_hints_silent(tmp_path, pattern):
    assert (
        _findings(tmp_path, f'import re\nre.search(r"{pattern}", "")\n') == []
    )


def test_regex_rules_ignore_non_re_calls(tmp_path):
    source = 'def compile(x):\n    return x\n\n\ncompile("[A-Za-z0-9_]+")\n'
    assert _findings(tmp_path, source) == []


# --------------------------------------------------------------------------- #
# S1192 — дубли литералов
# --------------------------------------------------------------------------- #
def test_s1192_found(tmp_path):
    source = 'A = "Notes (conflicts):"\nB = "Notes (conflicts):"\nC = "Notes (conflicts):"\n'
    findings = _findings(tmp_path, source)
    assert _keys(findings) == ["S1192"]
    assert "duplicated 3 times" in findings[0]


@pytest.mark.parametrize(
    "source",
    [
        'A = "Notes (conflicts):"\nB = "Notes (conflicts):"\n',  # только два раза
        'A = "utf-8"\nB = "utf-8"\nC = "utf-8"\n',  # похоже на идентификатор
        'A = "abc"\nB = "abc"\nC = "abc"\n',  # короче MIN_LEN
    ],
)
def test_s1192_silent(tmp_path, source):
    assert _findings(tmp_path, source) == []


def test_s1192_ignores_docstrings_and_annotations(tmp_path):
    source = (
        '"""Повтор строки ниже."""\n\n\n'
        'def one() -> "Повтор строки ниже.":\n'
        '    """Повтор строки ниже."""\n'
        "    return None\n"
    )
    assert _findings(tmp_path, source) == []


# --------------------------------------------------------------------------- #
# S5713 — лишний класс в кортеже except
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("handler", "redundant"),
    [
        ("except (PermissionError, OSError):", "PermissionError"),  # подкласс
        (
            "except (OSError, PermissionError):",
            "PermissionError",
        ),  # порядок не важен
        ("except (ValueError, json.JSONDecodeError):", "json.JSONDecodeError"),
        (
            "except (asyncio.TimeoutError, TimeoutError):",
            "TimeoutError",
        ),  # один и тот же объект
    ],
)
def test_s5713_found(tmp_path, handler, redundant):
    source = (
        f"import asyncio\nimport json\n\ntry:\n    pass\n{handler}\n    pass\n"
    )
    findings = [f for f in _findings(tmp_path, source) if " S5713 " in f]
    assert len(findings) == 1
    assert redundant in findings[0]


@pytest.mark.parametrize(
    "handler",
    [
        "except (ValueError, OSError):",  # не родственники
        "except (asyncio.CancelledError, asyncio.TimeoutError):",
        "except (ValueError, httpx.HTTPError):",  # сторонний пакет не резолвим
        "except OSError:",  # не кортеж
    ],
)
def test_s5713_silent(tmp_path, handler):
    source = f"import asyncio\nimport httpx\n\ntry:\n    pass\n{handler}\n    pass\n"
    assert [f for f in _findings(tmp_path, source) if " S5713 " in f] == []


def test_s5713_resolves_aliases(tmp_path):
    source = "from json import JSONDecodeError as JDE\n\ntry:\n    pass\nexcept (ValueError, JDE):\n    pass\n"
    findings = [f for f in _findings(tmp_path, source) if " S5713 " in f]
    assert len(findings) == 1 and "JDE" in findings[0]


# --------------------------------------------------------------------------- #
# S3516 — всегда один и тот же return
# --------------------------------------------------------------------------- #
def test_s3516_found(tmp_path):
    source = "def run(flag):\n    if flag:\n        return 0\n    print(flag)\n    return 0\n"
    assert _keys(_findings(tmp_path, source)) == ["S3516"]


@pytest.mark.parametrize(
    "source",
    [
        "def run(flag):\n    if flag:\n        return 1\n    return 0\n",  # разные значения
        "def run(flag):\n    if flag:\n        return None\n    return None\n",  # None не считается
        "def run(flag):\n    if flag:\n        return 0\n",  # неявный return None
        "def run(flag):\n    for x in flag:\n        yield x\n",  # генератор
        "def run(flag):\n    def inner():\n        return 0\n\n    if flag:\n        return 0\n    return inner\n",
    ],
)
def test_s3516_silent(tmp_path, source):
    assert [f for f in _findings(tmp_path, source) if " S3516 " in f] == []


def test_s3516_counts_raise_as_exit(tmp_path):
    source = "def run(flag):\n    if flag:\n        raise ValueError(flag)\n    if flag is None:\n        return 2\n    return 2\n"
    assert _keys(_findings(tmp_path, source)) == ["S3516"]


# --------------------------------------------------------------------------- #
# S3358 — тернарный внутри тернарного
# --------------------------------------------------------------------------- #
def test_s3358_found(tmp_path):
    assert _keys(_findings(tmp_path, "x = 1 if a else (2 if b else 3)\n")) == [
        "S3358"
    ]


@pytest.mark.parametrize(
    "source", ["x = 1 if a else 2\n", "x = [1 if a else 2, 3 if b else 4]\n"]
)
def test_s3358_silent(tmp_path, source):
    assert _findings(tmp_path, source) == []


# --------------------------------------------------------------------------- #
# S4144 — одинаковые тела функций
# --------------------------------------------------------------------------- #
IDENTICAL_BODY = "    value = raw.strip()\n    return value or None\n"


def test_s4144_found(tmp_path):
    source = f"def first(raw):\n{IDENTICAL_BODY}\n\ndef second(raw):\n{IDENTICAL_BODY}"
    findings = _findings(tmp_path, source)
    assert _keys(findings) == ["S4144"]
    assert "identical to first" in findings[0]


def test_s4144_compares_only_siblings(tmp_path):
    """Одинаковые замыкания внутри разных функций — приём, а не дубль."""
    nested = IDENTICAL_BODY.replace("    ", "        ")
    source = (
        f'def outer_one(raw):\n    log("первый")\n    def inner(raw):\n{nested}'
        f"    return inner\n\n\n"
        f'def outer_two(raw):\n    log("второй")\n    def inner(raw):\n{nested}'
        f"    return inner\n"
    )
    assert [f for f in _findings(tmp_path, source) if " S4144 " in f] == []


def test_s4144_ignores_one_liners(tmp_path):
    source = "def first(raw):\n    return raw\n\n\ndef second(raw):\n    return raw\n"
    assert _findings(tmp_path, source) == []


def test_s4144_sees_methods_of_one_class(tmp_path):
    body = "        value = raw.strip()\n        return value or None\n"
    source = f"class Holder:\n    def first(self, raw):\n{body}\n    def second(self, raw):\n{body}"
    assert _keys(_findings(tmp_path, source)) == ["S4144"]


# --------------------------------------------------------------------------- #
# S108 — пустой блок
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source",
    [
        "if flag:\n    pass\n",
        "for item in items:\n    pass\n",
        "while flag:\n    pass\n",
        "try:\n    run()\nfinally:\n    pass\n",
    ],
)
def test_s108_found(tmp_path, source):
    assert _keys(_findings(tmp_path, source)) == ["S108"]


@pytest.mark.parametrize(
    "source",
    [
        "if flag:\n    # ждём следующего хода\n    pass\n",  # комментарий объясняет пустоту
        "if flag:\n    pass  # ждём следующего хода\n",  # хвостовой комментарий — тоже объяснение
        'if flag:\n    run("# не комментарий, а строка")\n',  # решётка внутри литерала не в счёт
        "try:\n    run()\nexcept OSError:\n    pass\n",  # except Sonar не считает
        "if flag:\n    run()\n",
        "class Empty:\n    pass\n",  # класс — не блок кода
    ],
)
def test_s108_silent(tmp_path, source):
    assert [f for f in _findings(tmp_path, source) if " S108 " in f] == []


# --------------------------------------------------------------------------- #
# S5527 — выключенная проверка TLS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source",
    [
        "import ssl\n\nctx = ssl.create_default_context()\nctx.check_hostname = False\n",
        "import ssl\n\nctx = ssl.create_default_context()\nctx.verify_mode = ssl.CERT_NONE\n",
        "import ssl\n\nctx = ssl._create_unverified_context()\n",
    ],
)
def test_s5527_found(tmp_path, source):
    assert "S5527" in _keys(_findings(tmp_path, source))


@pytest.mark.parametrize(
    "source",
    [
        "import ssl\n\nctx = ssl.create_default_context()\nctx.check_hostname = True\n",
        "import ssl\n\nctx = ssl.create_default_context()\nctx.verify_mode = ssl.CERT_REQUIRED\n",
    ],
)
def test_s5527_silent(tmp_path, source):
    assert _findings(tmp_path, source) == []


# --------------------------------------------------------------------------- #
# Web:S5256 — таблица без описания
# --------------------------------------------------------------------------- #
def test_table_without_caption_found(tmp_path):
    findings = _findings(
        tmp_path,
        "<html><body><table><tr><td>1</td></tr></table></body></html>",
        name="p.html",
    )
    assert _keys(findings) == ["Web:S5256"]


@pytest.mark.parametrize(
    "table",
    [
        "<table><caption>Пакеты</caption><tr><td>1</td></tr></table>",
        '<table aria-label="Пакеты"><tr><td>1</td></tr></table>',
        '<table summary="Пакеты"><tr><td>1</td></tr></table>',
    ],
)
def test_table_with_description_silent(tmp_path, table):
    assert (
        _findings(
            tmp_path, f"<html><body>{table}</body></html>", name="p.html"
        )
        == []
    )


# --------------------------------------------------------------------------- #
# Общее: NOSONAR, обход дерева
# --------------------------------------------------------------------------- #
def test_nosonar_silences_the_line(tmp_path):
    source = "import ssl\n\nctx = ssl._create_unverified_context()  # NOSONAR: opt-in пользователя\n"
    assert _findings(tmp_path, source) == []


def test_iter_files_skips_vendor_and_tests(tmp_path):
    for rel in (
        "src/app.py",
        "tests/test_app.py",
        "external/vendor.py",
        "src/conftest.py",
        "docs/page.html",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    found = {p.name for p in checker.iter_files([str(tmp_path)])}
    assert found == {"app.py", "page.html"}


def test_main_returns_zero_on_clean_tree(tmp_path, capsys):
    (tmp_path / "ok.py").write_text(
        "def run():\n    return 1\n", encoding="utf-8"
    )
    assert checker.main([str(tmp_path)]) == 0
    assert "0 finding(s)" in capsys.readouterr().out


def test_main_returns_one_on_finding(tmp_path, capsys):
    (tmp_path / "bad.py").write_text("if flag:\n    pass\n", encoding="utf-8")
    assert checker.main([str(tmp_path)]) == 1
    assert "S108" in capsys.readouterr().out


@pytest.mark.parametrize(
    "call",
    [
        "re.escape('[0-9]')",
        "re.escape('[A-Za-z0-9_]')",
        "re.escape(" + repr(CALIBRATION_REGEX) + ")",
    ],
)
def test_regex_escape_is_literal_input(tmp_path, call):
    assert _findings(tmp_path, "import re\n" + call) == []


@pytest.mark.parametrize(
    "call, expected",
    [
        ("re.compile('[0-9]', re.A)", True),
        ("re.search(pattern='[0-9]', string='', flags=re.A | re.I)", True),
        ("re.sub('[0-9]', '', '', 0, re.ASCII)", True),
        ("re.split('[0-9]', '', 0, re.A)", True),
        ("re.compile('(?a)[A-Za-z0-9_]+')", True),
        ("re.compile('(?a:[0-9])')", True),
        ("re.compile('(?a)(?u:[0-9])')", False),
        ("re.compile('(?u:[0-9])', re.A)", False),
        ("re.compile('[0-9]')", False),
        ("re.compile('[A-Za-z0-9_]', re.I)", False),
        ("re.compile('[0-9]', unknown_flags)", False),
        ("re.compile('[0-9]', **options)", False),
        (r"re.compile(r'\[0-9]', re.A)", False),
    ],
)
def test_regex_hints_preserve_character_set(tmp_path, call, expected):
    findings = _findings(tmp_path, "import re\n" + call)
    assert ("S6353" in _keys(findings)) is expected


def test_regex_unicode_ranges_have_different_semantics():
    import re

    for pattern, replacement, sample in [
        ("[0-9]+", r"\d+", "١٢"),
        ("[A-Za-z0-9_]+", r"\w+", "привет"),
    ]:
        assert re.fullmatch(pattern, sample) is None
        assert re.fullmatch(replacement, sample) is not None
        assert re.fullmatch(replacement, sample, re.A) is None


@pytest.mark.parametrize(
    "first, second, expected",
    [
        ("OSError", "ValueError", False),
        ("ValueError", "OSError", True),
    ],
)
def test_exception_imports_do_not_leak_between_functions(
    tmp_path, first, second, expected
):
    source = f"""def f():
    from builtins import {first} as E
    try:
        int('bad')
    except (ValueError, E):
        return 1

def g():
    from builtins import {second} as E
    return E
"""
    assert ("S5713" in _keys(_findings(tmp_path, source))) is expected


@pytest.mark.parametrize(
    "source",
    [
        "import xml.etree.ElementTree\ntry:\n    run()\nexcept (SyntaxError, xml.etree.ElementTree.ParseError):\n    pass\n",
        "import xml.etree.ElementTree as ET\ntry:\n    run()\nexcept (SyntaxError, ET.ParseError):\n    pass\n",
        "from json import JSONDecodeError as E\ndef f():\n    try:\n        run()\n    except (ValueError, E):\n        pass\n",
        "def outer():\n    from json import JSONDecodeError as E\n    def inner():\n        try:\n            run()\n        except (ValueError, E):\n            pass\n    return inner\n",
        "from builtins import ValueError as E\nclass C:\n    from builtins import OSError as E\n    def f(self):\n        try:\n            run()\n        except (ValueError, E):\n            pass\n",
    ],
)
def test_exception_imports_resolve_lexical_scope(tmp_path, source):
    assert "S5713" in _keys(_findings(tmp_path, source))


@pytest.mark.parametrize(
    "source",
    [
        "from builtins import ValueError as E\ndef f(E):\n    try:\n        run()\n    except (ValueError, E):\n        pass\n",
        "from builtins import ValueError as E\nE = custom\ntry:\n    run()\nexcept (ValueError, E):\n    pass\n",
        "def f():\n    if condition:\n        from builtins import ValueError as E\n    try:\n        run()\n    except (ValueError, E):\n        pass\n",
        "class C:\n    from builtins import ValueError as E\n    def f(self):\n        try:\n            run()\n        except (ValueError, E):\n            pass\n",
        "def f():\n    try:\n        run()\n    except (ValueError, E):\n        pass\n    from builtins import ValueError as E\n",
    ],
)
def test_exception_unknown_bindings_are_not_guessed(tmp_path, source):
    assert "S5713" not in _keys(_findings(tmp_path, source))


@pytest.mark.parametrize(
    "prefix, context", [("def", "with"), ("async def", "async with")]
)
def test_same_return_allows_suppressing_context_manager(
    tmp_path, prefix, context
):
    source = f"""{prefix} f(x):
    {context} manager():
        if x == 0:
            return 1
        if x == 1:
            return 1
        raise ValueError(x)
"""
    assert "S3516" not in _keys(_findings(tmp_path, source))


def test_same_return_after_context_still_reported(tmp_path):
    source = "def f(x):\n    with manager():\n        if x:\n            return 1\n    return 1\n"
    assert "S3516" in _keys(_findings(tmp_path, source))


@pytest.mark.parametrize(
    "source",
    [
        "if flag:\n    # explanation for the other branch\n    run()\nelse:\n    pass\n",
        "try:\n    # explanation for try\n    run()\nfinally:\n    pass\n",
        "for x in values:\n    # explanation for loop\n    run()\nelse:\n    pass\n",
        "if flag:\n    run()\n    # still belongs to if\nelse:\n    pass\n",
    ],
)
def test_empty_branch_cannot_borrow_another_comment(tmp_path, source):
    assert "S108" in _keys(_findings(tmp_path, source))


@pytest.mark.parametrize(
    "source",
    [
        "if flag:\n    run()\nelse:  # intentionally empty\n    pass\n",
        "try:\n    run()\nfinally:\n    # intentionally empty\n    pass\n",
        "if flag:\n    run()\nelse:\n    pass  # intentionally empty\n",
    ],
)
def test_empty_branch_owns_its_explanation(tmp_path, source):
    assert "S108" not in _keys(_findings(tmp_path, source))


@pytest.mark.parametrize(
    "source",
    [
        "result = 'NOSONAR' if a else (1 if b else 2)\n",
        "result = '# NOSONAR' if a else (1 if b else 2)\n",
    ],
)
def test_nosonar_in_data_does_not_suppress(tmp_path, source):
    assert "S3358" in _keys(_findings(tmp_path, source))


@pytest.mark.parametrize(
    "source, expected",
    [
        ('<table data-note="NOSONAR"></table>', True),
        ("<table></table><!-- NOSONAR: intentional -->", False),
        ("<!-- NOSONAR: another line -->\n<table></table>", True),
    ],
)
def test_html_nosonar_is_only_a_comment_on_the_same_line(
    tmp_path, source, expected
):
    assert (
        "Web:S5256" in _keys(_findings(tmp_path, source, name="sample.html"))
    ) is expected
