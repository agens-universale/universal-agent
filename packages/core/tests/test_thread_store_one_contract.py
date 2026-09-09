"""Контракт хранилища объявлен ОДИН раз — в ABC, — и выводится из него.

Раньше те же одиннадцать методов были записаны трижды: в ``ThreadStore``
(``Protocol``), в ``ThreadStoreBase`` (ABC) и отдельным кортежем
``CONTRACT_METHODS_HINT``. Согласие не сторожил никто: метод, добавленный в ABC
и забытый в кортеже, ядро начало бы звать у хранилищ, которые о нём не знают, —
а диспетчер пропустил бы их, сверяясь со старым списком.

Протокол снят, кортеж выводится из абстрактных методов ABC. Здесь проверяется,
что вывод остался выводом, а не превратился обратно в копию, и что снятие
протокола НЕ закрыло утиный путь: диспетчер по-прежнему смотрит на имена, а не
на наследование.
"""
import pytest

from gc_core.thread_store_async import athread_store
from gc_core.thread_store_base import (
    ASYNC_CONTRACT_METHODS,
    CONTRACT_METHODS,
    AsyncThreadStoreBase,
    ThreadStoreBase,
)


def test_the_contract_is_derived_from_the_abc():
    """Кортеж = абстрактные методы ABC. Не «совпадает», а именно выводится."""
    assert set(CONTRACT_METHODS) == set(ThreadStoreBase.__abstractmethods__), (
        "список методов разошёлся с ABC — значит его снова написали руками"
    )
    assert len(CONTRACT_METHODS) == len(set(CONTRACT_METHODS)), "дубликаты в списке"
    assert CONTRACT_METHODS, "контракт пуст — вывод сломался"


def test_the_order_is_the_declaration_order():
    """Порядок — как в ABC, а не случайный.

    ``__abstractmethods__`` это frozenset: его порядок меняется между
    запусками, а имена попадают в сообщения об ошибках, где скачущий порядок
    мешает сравнивать два прогона глазами.
    """
    declared = [
        name for name, attr in ThreadStoreBase.__dict__.items()
        if getattr(attr, "__isabstractmethod__", False)
    ]
    assert list(CONTRACT_METHODS) == declared


def test_the_async_side_mirrors_the_sync_one():
    """Асинхронный контракт — те же имена с приставкой ``a``, без исключений."""
    assert set(ASYNC_CONTRACT_METHODS) == {f"a{name}" for name in CONTRACT_METHODS}
    assert set(ASYNC_CONTRACT_METHODS) == set(AsyncThreadStoreBase.__abstractmethods__)


def test_the_protocol_is_gone():
    """Второго объявления быть не должно — ради этого всё и делалось."""
    import gc_core.thread_store_types as types_mod

    assert not hasattr(types_mod, "ThreadStore"), (
        "класс-протокол вернулся: контракт снова объявлен дважды"
    )
    assert not hasattr(types_mod, "CONTRACT_METHODS_HINT"), (
        "список методов вернулся в модуль типов — он выводится из ABC"
    )


def test_removing_the_protocol_did_not_close_the_duck_path():
    """Проверка ОСТАЛАСЬ структурной: наследование не обязательно.

    Это главное последствие снятия протокола, за которым надо следить.
    Хранилище, которое наследовать не может — прокси на ``__getattr__``,
    объект из чужого DI-контейнера, — принимается по именам методов, как и
    раньше. Если диспетчер начнёт смотреть на ABC, такие хранилища отвалятся
    молча, а сообщение будет про «не годится», а не про наследование.
    """
    from gc_testkit.memory_thread_store import MemoryThreadStore

    inner = MemoryThreadStore()

    class _Proxy:
        def __getattr__(self, name):
            return getattr(inner, name)

    proxy = _Proxy()
    assert not isinstance(proxy, ThreadStoreBase), "проверка пустая: прокси наследует"

    assert athread_store(proxy) is not None, (
        "утиное хранилище перестало приниматься — ABC стал гейтом"
    )


def test_a_store_missing_one_method_is_still_refused():
    """Утиный путь не значит «пускаем всех»: неполное хранилище отвергается."""
    from gc_core.thread_store_async import ThreadStoreContractError
    from gc_testkit.memory_thread_store import MemoryThreadStore

    inner = MemoryThreadStore()
    missing = CONTRACT_METHODS[-1]

    class _Incomplete:
        def __getattr__(self, name):
            if name == missing:
                raise AttributeError(name)
            return getattr(inner, name)

    with pytest.raises(ThreadStoreContractError, match=missing):
        athread_store(_Incomplete())
