"""`task_id` и `system` не должны уезжать в путь файловой системы.

Что было. `_make_swarm_id` делал лишь strip/lower, поэтому
`task_id="../../../../victim"` давал `swarm_generic_../../../../victim`, а
`normpath` схлопывал это наружу из каталога песочницы. Клиент управлял путём, по
которому потом шли: `makedirs`, запись `meta.json`, материализация
`.gc/config.yml`, `--bind … /work` внутрь песочницы (то есть монтирование НА
ЗАПИСЬ), `shutil.rmtree` при провале запуска и чтение файла обычным GET через
`/swarm/status` и `/swarm/logs`.

Защита `sandbox_created = not os.path.isdir(sandbox_dir)` не спасала: в пути есть
литеральный сегмент `swarm_generic_..`, которого на диске нет, поэтому `isdir`
возвращал False даже когда цель существует.

Класс дыры унаследован с develop; наш только `rmtree`, добавленный при починке
«провал запуска не должен оставлять песочницу», — и он же наименее опасный из
шести стоков, потому что единственный условный.

Чиним НА ВХОДЕ, а не в `_make_swarm_id`: read-эндпоинты собирают идентификатор
инлайном, мимо него.
"""

from __future__ import annotations

import pytest
from gc_core.thread_store import sanitize_system_id
from gc_server.rest_api.utils import ensure_task_id, normalize_system


@pytest.mark.parametrize(
    "bad",
    [
        "../../../../victim",
        "..",
        "a/b",
        "a.b",
        "2026-09-01/run-7",
        "x\x00y",
    ],
)
def test_traversal_task_id_is_rejected(bad):
    """Отказ, а не коэрсинг: склеивать разные задачи в один тред нельзя.

    Подмена, которую ловит тест: вернуть в `ensure_task_id` прежний
    `task_id = raw.strip()`.
    """
    with pytest.raises(ValueError):
        ensure_task_id({"task_id": bad})


@pytest.mark.parametrize("good", ["PROJ-123", "build-42", "2026-09-01_run-7", "abc"])
def test_legitimate_task_id_still_passes(good):
    """Положительный контроль: живые формы не сломаны.

    Без этой половины тест прошёл бы и на реализации «отвергать всё подряд».
    Формы с `/` и `.` в список не входят намеренно — они и раньше падали ниже по
    потоку, в `thread_store`, просто позже и грязнее.
    """
    assert ensure_task_id({"task_id": good}) == good


def test_generated_task_id_is_valid():
    """Сгенерированный uuid обязан проходить собственную валидацию.

    Иначе запрос без `task_id` начал бы падать 400 на ровном месте.
    """
    payload: dict = {}
    generated = ensure_task_id(payload)
    assert payload["task_id"] == generated
    assert ensure_task_id({"task_id": generated}) == generated


def test_traversal_system_is_coerced_not_rejected():
    """`system` нормализуется: там законны пробелы и регистр.

    Отказ сломал бы живых клиентов, а `../` обязан перестать быть путём.
    Подмена: убрать `sanitize_system_id` вокруг `normalize_system` в /swarm/run.
    """
    assert sanitize_system_id(normalize_system("My System", default_system="generic")) == "my_system"

    coerced = sanitize_system_id(normalize_system("../../../../victim", default_system="generic"))
    assert "/" not in coerced and ".." not in coerced, coerced
