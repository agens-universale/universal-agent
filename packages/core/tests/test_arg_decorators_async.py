"""``parse_json_object_argument`` умеет оборачивать корутинную функцию.

Декоратор писался, когда все тулы были синхронными, и его обёртка возвращает
``func(self, …)`` как есть. Для корутины это работало бы на СЧАСТЛИВОМ пути —
вызывающий получил бы корутину и дождался её, — но на ошибочном декоратор
возвращает готовый словарь, минуя ``func``. Диспетчер тулов делает
``await acall(...)`` (``tools/__init__.py``, ветка ``await acall``), и ``await``
над словарём — это ``TypeError`` вместо аккуратного отказа с полем ``error``.

Поэтому у декоратора две ветки, как у ``dual_wrap``
(``security/dual_wrapper.py``): корутинную функцию оборачиваем корутиной,
обычную — обычной.
"""
from gc_core.utils.arg_decorators import parse_json_object_argument


class _Tool:
    """Двойник тула: обе точки входа с одним и тем же декоратором."""

    def _json_arg_error(self, exc, kwargs):
        return {"ok": False, "error": {"field": exc.field}}

    @parse_json_object_argument(
        source_arg="args", target_arg="args_data",
        error_handler_method="_json_arg_error",
    )
    def call_sync(self, args=None, args_data=None, **kwargs):
        return {"ok": True, "parsed": args_data}

    @parse_json_object_argument(
        source_arg="args", target_arg="args_data",
        error_handler_method="_json_arg_error",
    )
    async def call_async(self, args=None, args_data=None, **kwargs):
        return {"ok": True, "parsed": args_data}


async def test_async_happy_path_parses_and_awaits():
    """Корутина под декоратором ждётся и получает разобранный аргумент."""
    out = await _Tool().call_async(args='{"x": 1}')
    assert out == {"ok": True, "parsed": {"x": 1}}


async def test_async_error_path_is_awaitable_too():
    """Главная проверка: отказ приходит значением, а не ломает ``await``.

    До правки декоратор отдавал словарь из синхронной обёртки, и вызывающий,
    написавший ``await``, падал с ``TypeError: object dict can't be used in
    'await' expression`` — вместо понятного отказа тула.
    """
    out = await _Tool().call_async(args="не json")
    assert out["ok"] is False
    assert out["error"]["field"] == "args"


def test_sync_path_unchanged():
    """Синхронная ветка осталась прежней — и на разборе, и на отказе."""
    tool = _Tool()
    assert tool.call_sync(args='{"y": 2}') == {"ok": True, "parsed": {"y": 2}}
    assert tool.call_sync(args="не json")["ok"] is False


async def test_async_without_the_source_arg_passes_through():
    """Аргумента нет — декоратор не вмешивается, но корутину всё равно ждёт."""
    assert await _Tool().call_async() == {"ok": True, "parsed": None}
