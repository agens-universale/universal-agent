"""Изоляция накопленной Python-кучи между живыми наборами Temporal."""

import gc

import pytest


@pytest.fixture(scope="module", autouse=True)
def isolate_previous_modules_gc_heap():
    """Сборщик текущего модуля не обходит объекты всех предыдущих тестов.

    Workflow заново импортирует ядро в sandbox. В полном наборе накопленная
    куча давала паузы GC до 6,3 с: истекал heartbeat 5 с и возникали лишние
    попытки activity; возможен и watchdog workflow 2 с. Отдельно эти сценарии
    проходят. Собираем мусор и временно исключаем старую кучу из обхода ДО
    запуска workers. Автосборка и явные collect внутри тестов продолжают
    работать с новыми объектами; таймауты Temporal не меняются.
    """
    gc.collect()
    gc.freeze()
    try:
        yield
    finally:
        gc.unfreeze()
        gc.collect()
