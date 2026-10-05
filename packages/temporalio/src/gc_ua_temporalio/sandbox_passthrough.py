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
    import httpx  # noqa: F401
    import mcp.client.session
    import mcp.client.sse
    import mcp.client.stdio
    import mcp.client.streamable_http
    import mcp.shared.exceptions  # noqa: F401
    import pydantic  # noqa: F401
    import readability  # noqa: F401
    import trafilatura  # noqa: F401
