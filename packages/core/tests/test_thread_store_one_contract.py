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


# --- дрейф умолчаний: два драйвера над одним порядком ввода-вывода ----------

def _io_sequences_of_bases() -> "tuple[dict[str, list[str]], dict[str, list[str]]]":
    """Последовательность обращений к хранилищу у каждого умолчания — по AST.

    Читается ИСХОДНИК, а не объекты: сравнивать надо именно порядок операций в
    теле, а по объекту его не видно. Считаются только вызовы вида
    ``self.<имя>(…)``, где ``<имя>`` объявлено в синхронной базе — то есть
    обращения к своему же контракту. Чистые помощники (``stamp_meta``,
    ``merge_runtime_meta``) живут в ``_ThreadStoreInvariants`` и сюда не
    попадают.

    Эталон имён ВЫВОДИТСЯ из синхронной базы, а не пишется списком: приставка
    ``a`` снимается тогда и только тогда, когда получившееся имя у неё есть.
    """
    import ast
    import pathlib
    import re

    import gc_core.thread_store_base as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))

    def methods(class_name):
        cls = next(
            n for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == class_name
        )
        return [m for m in cls.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]

    sync_methods = methods("ThreadStoreBase")
    sync_names = {m.name for m in sync_methods}

    def unprefix(name: str) -> str:
        m = re.match(r"^(_*)a(.+)$", name)
        if not m:
            return name
        base = m.group(1) + m.group(2)
        return base if base in sync_names else name

    def sequences(members):
        out = {}
        for member in members:
            ops = []
            for node in ast.walk(member):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute)
                        and isinstance(fn.value, ast.Name)
                        and fn.value.id == "self"):
                    continue
                name = unprefix(fn.attr)
                if name in sync_names:
                    ops.append(name)
            out[unprefix(member.name)] = ops
        return out

    return sequences(sync_methods), sequences(methods("AsyncThreadStoreBase"))


def test_default_implementations_keep_the_same_io_order():
    """Умолчания двух ABC обращаются к хранилищу в ОДНОМ порядке.

    Сжать эти два тела в одно нельзя: ``ThreadStoreBase`` — публичный
    синхронный контракт для встраивающих, ``AsyncThreadStoreBase`` — для тех,
    у кого ``redis.asyncio`` / ``asyncpg``; оба обязаны существовать. Чистые
    части уже вынесены в ``_ThreadStoreInvariants``, и различается ровно
    последовательность обращений — единственное, что ещё может разъехаться.

    Цена расхождения измерима и уже случалась (см. докстроку модуля): у
    ``replace_messages`` порядок ``reset`` → ``append_messages`` →
    ``save_meta`` → восстановление трассы держит два свойства сразу — тред не
    исчезает целиком между ``reset`` и ``save_meta``, и трасса субагентов
    переживает компакцию. Переставленный или потерянный шаг ломает то или
    другое молча: конформанс-набор проверяет РЕЗУЛЬТАТ каждой операции по
    отдельности, а порядок внутри умолчания — нет.
    """
    sync_io, async_io = _io_sequences_of_bases()

    assert set(sync_io) == set(async_io), (
        "наборы методов у двух баз разошлись: "
        f"только синхронные {sorted(set(sync_io) - set(async_io))}, "
        f"только асинхронные {sorted(set(async_io) - set(sync_io))}"
    )
    for name in sorted(sync_io):
        assert sync_io[name] == async_io[name], (
            f"{name}: порядок обращений к хранилищу разошёлся.\n"
            f"  синхронный:   {sync_io[name]}\n"
            f"  асинхронный:  {async_io[name]}"
        )

    # Проверка не должна быть пустой: хотя бы у одного умолчания
    # последовательность непустая, иначе сравнивать нечего и тест зеленеет
    # на любом изменении.
    assert any(ops for ops in sync_io.values()), (
        "ни одного обращения к контракту не найдено — сборщик последовательностей "
        "перестал их видеть, и сторож стал холостым"
    )
