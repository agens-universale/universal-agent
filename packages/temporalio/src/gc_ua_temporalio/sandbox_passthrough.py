"""Сторонние пакеты, которые песочница workflow берёт из процесса воркера.

Ядро импортирует HTTP-клиент, MCP SDK и разбор HTML (trafilatura,
readability) на уровне модулей (как в develop):
транспорты нужны локальному запуску, а workflow до них не доходит — модель,
тулы и MCP исполняются в activities. Импортировать эти пакеты внутри песочницы
нельзя: при загрузке они трогают ``urllib.request`` и прочие запрещённые
модули. Поэтому они пропускаются как есть (passthrough), а собственный код
ядра по-прежнему загружается в песочнице и проверяется её ограничениями.

Модуль импортируют модули workflow до первого импорта ядра.
"""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    # httpcore httpx грузит лениво — при первом клиенте; модуль ядра создаёт
    # клиент при импорте (`utils.http_session`), поэтому пропускаем и его.
    import httpcore  # noqa: F401
    import httpx  # noqa: F401
    import mcp.client.session
    import mcp.client.sse
    import mcp.client.stdio
    import mcp.client.streamable_http
    import mcp.shared.exceptions  # noqa: F401
    import pydantic  # noqa: F401
    import readability  # noqa: F401
    import trafilatura  # noqa: F401

    # Остальные сторонние библиотеки, которые ядро импортирует напрямую.
    # В песочнице каждый запуск workflow импортировал бы их заново, а копии
    # attrs/jsonschema удерживаются после конца workflow: +1,4 МБ RSS на
    # сессию (замер 06.10, 600 сессий). Состояния workflow в них нет.
    import attr  # noqa: F401
    import attrs  # noqa: F401
    import chardet  # noqa: F401
    import dotenv  # noqa: F401
    import idna  # noqa: F401
    import jsonschema  # noqa: F401
    import pydantic_settings  # noqa: F401
    import referencing  # noqa: F401
    import typing_inspection  # noqa: F401
    import yaml  # noqa: F401
