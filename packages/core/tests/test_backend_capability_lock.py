"""Кэш возможностей бэкенда: кто его наполняет и на чём экономит.

Раньше здесь сторожили повторный захват ``_cap_lock``: ``grep_exclude_dir`` и
``grep_include`` брали замок и внутри читали ``has_grep``, который брал тот же
нереентерабельный замок, — холодное чтение зависало, и спасал только порядок
вызовов. Предмета больше нет: пробы стали корутинными, свойства читают ТОЛЬКО
кэш, и единственный его писатель — ``awarm_capabilities``. Замок снят вместе с
ленивой пробой из свойства.

Осталось два свойства, которые стоит держать: под-возможности проверяются
пробой, когда grep рабочий, и не проверяются, когда его нет.
"""

import pytest
from gc_core.backends import capabilities as capabilities
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import CapabilitiesNotWarmedError


def _stub(value, calls=None, name=""):
    """Двойник пробы повторяет ФОРМУ прода: проба корутинная.

    Синхронная заглушка дала бы ``TypeError: object bool can't be used in
    'await'``, а прогрев ловит исключение пробы и пишет ``False`` — то есть
    подмена молча превратилась бы в «возможности нет», и тест зеленел бы на
    ложном основании.
    """

    async def probe(_backend):
        if calls is not None:
            calls.append(name)
        return value

    return probe


async def test_sub_capabilities_are_probed_when_grep_is_usable(monkeypatch):
    """При рабочем grep под-возможности всё ещё проверяются пробой."""
    monkeypatch.setattr(capabilities, "aprobe_has_grep", _stub(True))
    monkeypatch.setattr(capabilities, "aprobe_grep_include", _stub(True))
    monkeypatch.setattr(capabilities, "aprobe_grep_exclude_dir", _stub(True))
    backend = LocalFsBackend()

    await backend.awarm_capabilities()

    assert backend.grep_include is True
    assert backend.grep_exclude_dir is True


async def test_sub_capabilities_are_skipped_when_grep_is_missing(monkeypatch):
    """Нет grep — под-пробы не запускаются вовсе, но кэш всё равно закрыт.

    Два свойства сразу. Первое: два запуска процесса на боксе, где ответ уже
    известен, — чистая плата (в песочнице это до ~10 с на пробу). Второе: кэш
    обязан закрыться значением, а не остаться пустым, иначе чтение свойства
    ПОСЛЕ прогрева получило бы отказ «не прогрето» — самый сбивающий с толку
    из возможных ответов.
    """
    calls: list[str] = []
    monkeypatch.setattr(capabilities, "aprobe_has_grep", _stub(False, calls, "grep"))
    monkeypatch.setattr(capabilities, "aprobe_grep_include", _stub(True, calls, "include"))
    monkeypatch.setattr(capabilities, "aprobe_grep_exclude_dir", _stub(True, calls, "exclude"))
    backend = LocalFsBackend()

    await backend.awarm_capabilities()

    assert calls == ["grep"], f"под-пробы запускались напрасно: {calls}"
    assert backend.grep_include is False
    assert backend.grep_exclude_dir is False


async def test_capability_values_are_cached_once(monkeypatch):
    """Второй прогрев проб не повторяет: кэш наполняется однократно."""
    calls: list[str] = []
    monkeypatch.setattr(capabilities, "aprobe_has_grep", _stub(False, calls, "grep"))
    backend = LocalFsBackend()

    await backend.awarm_capabilities()
    await backend.awarm_capabilities()

    assert calls == ["grep"], f"проба запускалась повторно: {calls}"
    assert backend.has_grep is False


def test_a_cold_read_refuses_instead_of_probing(monkeypatch):
    """Холодное чтение — отказ, а не проба.

    Именно здесь была заморозка: свойство запускало пробу само, из-под loop
    вызывающего.
    """
    monkeypatch.setattr(
        capabilities,
        "aprobe_has_grep",
        _stub(True),
    )
    backend = LocalFsBackend()

    with pytest.raises(CapabilitiesNotWarmedError):
        backend.grep_include
