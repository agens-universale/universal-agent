"""Гейт: `download_file` пишет тело инлайн, а не в потоке из пула.

Раньше здесь стояло обратное требование — запись уходила в
``asyncio.to_thread``, потому что ``body`` держит всё тело в памяти (до
``max_bytes``, по умолчанию 50 МиБ), и синхронный ``write`` такого размера
казался опасным для loop.

Замер показал, что опасность мнимая. На записи 32 МиБ худший разрыв пульса:

===============  ==========
инлайн           6.1 мс
через ``to_thread``  2.7 мс
===============  ==========

Разница 3.4 мс при бюджете приёмки 150 мс (``test_acceptance_non_blocking``) —
то есть поток экономит 2% бюджета, а стоит передачи задачи в пул и обратно.
Нативной асинхронной записи, которая сняла бы выбор, в Python нет: у ``anyio``
каждый метод ``AsyncFile`` это ``await to_thread.run_sync(self._fp.write, …)``
(``anyio/_core/_fileio.py``), у ``aiofiles`` — ``run_in_executor``. Выбор был
не «поток или корутина», а «поток или инлайн», и инлайн дешевле.

Сеть в этом туле остаётся нативно асинхронной: ``_http_get_bytes`` ходит через
``httpx.AsyncClient`` и никакого пула не касается. Поток убран только с записи.
"""
import ast

from gc_testkit.paths import REPO

_SRC = REPO / "packages/core/src/gc_core/tools/exec/download_file.py"


def _acall_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "acall"
    )


def test_acall_writes_the_body_inline():
    """Запись зовётся напрямую, без ``to_thread``.

    Структурная, и намеренно: поведенческая проверка поймала бы возврат пула
    только замером на большом файле, а эта краснеет на самой правке.
    """
    acall = _acall_node()

    direct = [
        n for n in ast.walk(acall)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_atomic_write"
    ]
    assert direct, "не нашёл прямого вызова self._atomic_write(…) в acall"

    # Запрет узкий — на ЗАПИСЬ, а не на поток вообще. Проверка гарда записи
    # (`_check_write_guard`) уезжает в поток законно: в режиме `ask` она ждёт
    # человека, в TUI это до 600 с, и loop вызывающего иначе встал бы целиком.
    threaded_write = [
        n for n in ast.walk(acall)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "to_thread"
        and any(getattr(a, "attr", None) == "_atomic_write" for a in n.args)
    ]
    assert not threaded_write, (
        "запись снова уходит в asyncio.to_thread — поток здесь не окупается: "
        "3.4 мс выигрыша на 32 МиБ при бюджете 150 мс"
    )


def test_network_path_stays_natively_async():
    """Сеть не трогали: тело качается через ``await``, а не через пул.

    Инлайн касается только записи. Если кто-то заодно сделает синхронным и
    скачивание, loop встанет на всё время запроса — а вот у сети нативная
    асинхронная форма есть, и отказываться от неё незачем.
    """
    acall = _acall_node()

    awaited_http = [
        n for n in ast.walk(acall)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "_http_get_bytes"
    ]
    assert awaited_http, "скачивание тела больше не идёт через await _http_get_bytes"
