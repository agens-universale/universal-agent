"""Файловый I/O конфига идёт инлайн — ни потока, ни пула.

Свойство, а не стиль. У файлов нет нативной асинхронной формы: всё, что
выглядит как «async file», — обёртка над тем же пулом (у `anyio` каждый метод
`AsyncFile` это буквально `await to_thread.run_sync(...)`, у `aiofiles` —
`run_in_executor`). Единственный настоящий нативный путь, `io_uring`,
существует только на Linux и требует C-расширения.

Значит выбор здесь не «поток или корутина», а «поток или инлайн». Обычные
чтение и запись отвечают за микросекунды, а поход в общий пул стоит 40 мкс
(замерено) — дороже самой работы. Плюс пул ОБЩИЙ: туда же уходят разбор HTML
и чужие синхронные бэкенды, и конфиг вставал бы к ним в очередь.

Почему нужен сторож, а не докстринг. «На всякий случай в поток» — типовая
правка: она выглядит безобидно, тесты от неё зеленеют, а замедление
размазывается по всему прогону и не приписывается причине. До этого гейта
свойство держалось на комментариях в четырёх файлах.
"""

import ast

import pytest
from gc_testkit.paths import REPO

_SRC = REPO / "packages" / "core" / "src" / "gc_core"

#: Модули, которые ходят в файлы конфига и обязаны делать это инлайн.
CONFIG_IO_MODULES = [
    "config_modules/layers.py",
    "config_modules/profiles.py",
    "config_modules/registry_write.py",
    "config.py",
    "config_raw.py",
    "utils/user_text.py",
    "utils/file_text.py",
]

#: Имена, любое из которых означает уход работы с текущего потока.
OFFLOADERS = {
    "to_thread",
    "run_in_executor",
    "run_abandonable",
    "run_coro_blocking",
}


def _offload_calls(path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (
            fn.attr
            if isinstance(fn, ast.Attribute)
            else getattr(fn, "id", None)
        )
        if name in OFFLOADERS:
            found.append(f"{path.name}:{node.lineno}: {name}(...)")
    return found


@pytest.mark.parametrize("rel", CONFIG_IO_MODULES)
def test_config_module_does_not_offload_file_io(rel):
    path = _SRC / rel
    assert path.is_file(), (
        f"модуль переехал — поправьте CONFIG_IO_MODULES: {rel}"
    )

    offenders = _offload_calls(path)
    assert offenders == [], (
        f"{rel} уводит работу с потока:\n  " + "\n  ".join(offenders) + "\n"
        "У файлового I/O нативной асинхронной формы нет, и «async file» любой "
        "библиотеки — тот же пул. Переход в него стоит 40 мкс при работе в "
        "микросекунды, а сам пул общий с разбором HTML и чужими бэкендами."
    )


def test_the_module_list_is_not_stale():
    """Список закрыт намеренно — проверяем, что все имена ещё существуют.

    Обратная сторона закрытого списка: файл переехал, гейт молча перестал
    что-либо стеречь.
    """
    missing = [rel for rel in CONFIG_IO_MODULES if not (_SRC / rel).is_file()]
    assert missing == [], f"этих модулей больше нет: {missing}"
