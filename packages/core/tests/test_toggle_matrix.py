"""Гейт: переезд на async ничего не поменял НИ ПРИ КАКОМ положении конфига.

Приёмка переезда до сих пор проверяла один конфиг — дефолтный
(``test_parity_smoke``). Но заявленный критерий шире: «работает как раньше»
должно держаться при любом положении переключателей, а их 83, и 66 проверяемы
без живой модели.

С чем сравниваем и почему не с master. У master другая раскладка (плоский
``src/`` против ``packages/*/src/``), 132 коммита отставания и нет
``gc_core.aio`` вовсе — SDK там сравнивать не с чем. Расхождения были бы в
подавляющем большинстве «за три недели поменяли фичу», а не «async сломал»:
шум вместо сигнала.

База — ``aa363554``: первый коммит переезда, где добавлен ТОЛЬКО харнесс, а
рантайм идентичен до-async ``c96f26ff``. Разница с нашей веткой — ровно переезд
и ничего больше. Эталон снят прогоном ЭТОЙ ЖЕ матрицы в worktree на том
коммите; пересчитывать его нельзя, он и есть точка отсчёта.

Что сравнивается: финальный ответ, дамп треда построчно, отобранные поля meta,
число обращений к бэкенду и текст отказа. Отказ — тоже наблюдаемое поведение и
тоже подлежит сверке: важно, что он ОДИНАКОВ по обе стороны, а не что его нет.

Как пересобрать эталон (только если он намеренно устарел)::

    git worktree add /tmp/preasync aa363554
    cp packages/core/tests/toggle_matrix.py /tmp/preasync/packages/core/tests/
    cd /tmp/preasync/packages/core/tests
    uv run --project /tmp/preasync python toggle_matrix.py <файл>
"""
import json
import re
from pathlib import Path

import pytest

from toggle_matrix import ALL_CASES, run_case

REFERENCE = (
    Path(__file__).parent / "fixtures" / "toggle_matrix_reference_aa363554.json"
)

#: Поля, по которым идёт сверка.
COMPARED = ("answer", "dump", "meta", "backend_calls", "error")

#: Метка времени ВНУТРИ строки. ``strip_volatile`` снимает только ключи словаря,
#: а в теле сообщения-сводки таймстемпы лежат внутри вложенного JSON
#: (``user_intent_log``) — то есть в обычной строке, куда он не достаёт.
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")


def _normalize(obj):
    """Убрать часы из значений, не трогая всё остальное."""
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    if isinstance(obj, str):
        return _TS.sub("<TS>", obj)
    return obj


@pytest.fixture(scope="module")
def reference() -> dict:
    data = json.loads(REFERENCE.read_text(encoding="utf-8"))
    return {row["id"]: row for row in data}


def test_reference_covers_every_case(reference):
    """Эталон и матрица не разъехались по составу.

    Без этой проверки добавленный случай молча остался бы непокрытым: сверка
    идёт по идентификаторам, и лишний просто не с чем было бы сравнивать.
    """
    assert set(reference) == {case.id for case in ALL_CASES}


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
def test_toggle_matches_pre_async(case, reference, tmp_path, monkeypatch):
    """Один прогон на положение переключателей — против до-async эталона."""
    expected = reference[case.id]
    actual = run_case(case, tmp_path, monkeypatch)

    for field in COMPARED:
        assert _normalize(actual.get(field)) == _normalize(expected.get(field)), (
            f"{case.id}: поле {field!r} разошлось с до-async прогоном"
            + (f"\n  ({case.note})" if case.note else "")
        )


def test_summary_cases_actually_compact(reference):
    """Случаи с включённым порогом ДЕЙСТВИТЕЛЬНО вызывают компакцию.

    Иначе матрица зелёная и бессмысленная: переключатель записан в конфиг, но
    ни на что не повлиял, и сравнивать нечего. Признак компакции — история
    схлопнулась в одно сообщение-сводку.
    """
    compacted = [
        cid for cid, row in reference.items()
        if len(row["dump"]) == 1
        and "dialog_summary" in json.dumps(row["dump"], ensure_ascii=False)
    ]
    assert compacted, "ни один случай не довёл дело до компакции"
    assert "summary/trigger_tiny" in compacted


def test_limit_cases_actually_hit_the_limit(reference):
    """Случай с ``max_steps=1`` действительно упирается в лимит.

    Та же мысль: без этого «лимит выставлен» не значит «лимит сработал».
    """
    row = reference["limits/max_steps_1"]
    baseline = reference["baseline/default"]
    assert len(row["dump"]) < len(baseline["dump"]), (
        "прогон с max_steps=1 дал столько же записей, сколько дефолтный — "
        "лимит не сработал, и случай ничего не проверяет"
    )
