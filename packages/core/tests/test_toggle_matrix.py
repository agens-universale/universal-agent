"""Гейт: переезд на async ничего не поменял НИ ПРИ КАКОМ положении конфига.

Приёмка переезда до сих пор проверяла один конфиг — дефолтный
(``test_parity_smoke``). Но заявленный критерий шире: «работает как раньше»
должно держаться при любом положении переключателей. Матрица трогает 124 листа
схемы из 163; остальные либо мертвы (нет читателя), либо исключены с
записанной причиной в ``NOT_IN_MATRIX``. Следит за этим
``test_every_live_schema_key_is_covered_or_explained``.

С чем сравниваем и почему не с master. Сравнение с master ВОЗМОЖНО и было
проделано: у него другая раскладка (плоский ``src/``) и в бэкенд он ходит
через ``requests``, а не httpx, поэтому харнесс нужен свой — но сценарий и
разбор дампа те же. Итог сверки на дефолтном конфиге: ответ совпал, дамп
разошёлся в ОДНОМ поле (``finish_gate.coverage_score``: 1.0 против 0.0) и
master сделал 4 обращения к бэкенду против 3.

Причина расхождения — НЕ переезд, и это установлено, а не предположено:
до-async эталон ``aa363554`` даёт ровно то же, что наша ветка (0.0 и три
обращения). Значит разница пришла из 132 коммитов между master и базой
переезда. Поэтому база сверки — ``aa363554``, а не master: там разница с нашей
веткой равна ровно переезду и ничему больше.

База — ``bf1cb021`` (``develop`` на момент вливания пакетов, 08.09.2026). До этого
базой был ``aa363554`` — первый коммит переезда, где добавлен ТОЛЬКО харнесс, а
рантайм идентичен до-async ``c96f26ff``. Пока ветка шла рядом с develop, тот
эталон и был точкой отсчёта: разница с нами равнялась ровно переезду.

После вливания он перестал ею быть, и это не протухание, а смена вопроса.
develop за три недели ПОМЕНЯЛ наблюдаемое поведение намеренно — например,
PR #92 добавил в ответ финализатора поле ``task``, и дамп разошёлся у всех 147
случаев на одном и том же месте. Сверять с эталоном, который этого поля не
знает, значит проверять, что мы не приняли чужую фичу.

Поэтому база теперь — сам ``develop``, а инвариант читается так: **асинхронный
прогон наблюдаемо неотличим от синхронного на том же коде**. Это ровно то
свойство, ради которого гейт и заведён; предыдущая формулировка была его частным
случаем, пока код был общим.

Эталон снят прогоном ЭТОЙ ЖЕ матрицы в worktree на develop. Пересчитывать его на
НАШЕЙ ветке нельзя — он станет отражением того, что мы сами и произвели.

Что сравнивается: финальный ответ, дамп треда построчно, отобранные поля meta,
число обращений к бэкенду и текст отказа. Отказ — тоже наблюдаемое поведение и
тоже подлежит сверке: важно, что он ОДИНАКОВ по обе стороны, а не что его нет.

Как пересобрать эталон (только если он намеренно устарел)::

    git worktree add /tmp/sync-base origin/develop
    cp packages/core/tests/toggle_matrix.py packages/core/tests/parity_harness.py \
       /tmp/sync-base/packages/core/tests/
    cd /tmp/sync-base/packages/core/tests
    PYTHONPATH=/tmp/sync-base/packages/core/src:/tmp/sync-base/packages/testkit/src \
        python toggle_matrix.py <файл>

``packages/core/src`` в PYTHONPATH — НЕ лишний, и это дорогая правка рецепта.
Без него ``gc_core`` берётся из установленного editable-пакета, то есть ИЗ
НАШЕЙ ВЕТКИ: скрипт исправно отработает, эталон запишется, и он будет
отражением того, что мы сами и произвели. Проверяется одной строкой::

    python -c "import gc_core; print(gc_core.__file__)"   # должен быть /tmp/sync-base

Установщик транспорта в ``parity_harness.install`` умеет обе ветки: у async это
фабрика ``get_async_client``, у синхронной — ``_NO_NETRC_SESSION``
(``ScriptedBackend`` держит совместимый с ``requests`` ``post`` ровно для этого).
"""
import json
import re
from pathlib import Path

import pytest

from toggle_matrix import ALL_CASES, run_case

#: Эталон снят на origin/develop (e66a1e90) тем же стендом.
#:
#: Раньше он снимался на merge-base bf1cb021 — «до асинхронности». Это
#: перестало работать, когда develop сам изменил поведение хода (PR «батч с
#: turn_finish — сначала вся work, закрытие по output»): матрица начала ловить
#: ЧУЖУЮ правку и краснела 144 случаями. Эталон обязан идти с той ветки, с
#: которой сравниваем, иначе гейт меряет не то, что заявляет.
REFERENCE = (
    Path(__file__).parent / "fixtures" / "toggle_matrix_reference_e66a1e90.json"
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
async def test_toggle_matches_pre_async(case, reference, tmp_path, monkeypatch):
    """Один прогон на положение переключателей — против до-async эталона."""
    expected = reference[case.id]
    actual = await run_case(case, tmp_path, monkeypatch)

    for field in COMPARED:
        assert _normalize(actual.get(field)) == _normalize(expected.get(field)), (
            f"{case.id}: поле {field!r} разошлось с до-async прогоном"
            + (f"\n  ({case.note})" if case.note else "")
        )


def test_summary_cases_actually_compact(reference):
    """Случаи с включённым порогом ДЕЙСТВИТЕЛЬНО вызывают компакцию.

    Иначе матрица зелёная и бессмысленная: переключатель записан в конфиг, но
    ни на что не повлиял, и сравнивать нечего.

    Признак компакции — сообщение-сводка в истории И история КОРОЧЕ, чем у
    того же сценария с выключенным порогом. Раньше здесь стояло
    ``len(dump) == 1``, но правка develop («батч с turn_finish — сначала вся
    work, закрытие по output») законно оставляет после сводки work и finish,
    и точное число перестало быть признаком компакции.
    """
    def _has_summary(row) -> bool:
        return "dialog_summary" in json.dumps(row["dump"], ensure_ascii=False)

    compacted = [cid for cid, row in reference.items() if _has_summary(row)]
    assert compacted, "ни один случай не довёл дело до компакции"
    assert "summary/trigger_tiny" in compacted

    baseline = len(reference["summary/trigger_off"]["dump"])
    for cid in compacted:
        assert len(reference[cid]["dump"]) < baseline, (
            f"{cid}: сводка есть, а история не короче непожатой "
            f"({len(reference[cid]['dump'])} против {baseline})"
        )


def test_every_live_schema_key_is_covered_or_explained(reference):
    """Каждый живой ключ схемы либо в матрице, либо исключён с причиной.

    Гейт на полноту. Без него матрица выглядит внушительно и незаметно
    отстаёт: первая редакция трогала 46 листов схемы из 163, и понять это
    можно было только пересчитав вручную.

    «Не покрыт» и «покрывать нечем» — разные вещи, и различать их обязан
    ``NOT_IN_MATRIX``, а не память того, кто однажды считал.
    """
    from pydantic import BaseModel

    from gc_core.config_modules.root import AppConfig
    from test_config_toggles_have_readers import KNOWN_UNREAD
    from toggle_matrix import NOT_IN_MATRIX

    def leaves(model, prefix=""):
        out = []
        for name, field in model.model_fields.items():
            ann, path = field.annotation, f"{prefix}{name}"
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                out.extend(leaves(ann, path + "."))
            else:
                out.append(path)
        return out

    # Плоское имя иногда отличается от имени в схеме — проекция переименовывает.
    ALIAS = {
        "backend.api_mode": "backend_api_mode",
        "logging.format": "logging_format",
        "logging.propagate": "logging_propagate",
    }
    touched = {p.split(".")[-1] for case in ALL_CASES for p in case.overrides}
    gap = [
        leaf for leaf in leaves(AppConfig)
        if leaf.split(".")[-1] not in touched
        and ALIAS.get(leaf, "") not in touched
        and leaf not in KNOWN_UNREAD
        and leaf not in NOT_IN_MATRIX
    ]
    assert gap == [], (
        "ключи схемы не покрыты матрицей и не объяснены:\n  "
        + "\n  ".join(sorted(gap))
        + "\nЛибо заведите случай в toggle_matrix, либо занесите в NOT_IN_MATRIX "
        "с причиной."
    )


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
