"""Фальшивый MCP-сервер по stdio: ждёт того, что клиент ПООБЕЩАЛ.

Воспроизводит механику инцидента, стоившего половины дня. Сервер читает
``initialize`` и смотрит на ``capabilities`` клиента:

* объявлена способность (``roots`` и т.п.) — сервер вправе прислать встречный
  запрос и ЖДАТЬ ответа. Он его шлёт и ждёт ``STALL_SECONDS``, потому что
  отвечать некому: обработчика входящих запросов у клиента нет;
* объявлено пусто — ждать нечего, ответ уходит сразу.

Живой ``@playwright/mcp`` ведёт себя ровно так; замер: ``{}`` → 1,1 с,
``roots`` → 61,2 с. Здесь ожидание укорочено до значения из переменной
окружения, чтобы тест был быстрым, но механика та же.
"""

import json
import os
import sys
import time

STALL_SECONDS = float(os.environ.get("STALL_SECONDS", "2.0"))


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        if method == "initialize":
            if os.environ.get("CHATTY"):
                # Незапрошенное уведомление ПЕРЕД ответом — так ведут себя
                # серверы с включённым логированием. Клиент, читающий первую
                # строку как ответ, примет за ответ вот это.
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/message",
                        "params": {"level": "info", "data": "поднимаюсь"},
                    }
                )
            declared = ((msg.get("params") or {}).get("capabilities")) or {}
            if declared:
                # Клиент пообещал — шлём встречный запрос и ждём ответа.
                send({"jsonrpc": "2.0", "id": 9001, "method": "roots/list"})
                deadline = time.monotonic() + STALL_SECONDS
                while time.monotonic() < deadline:
                    time.sleep(0.01)  # ответа не будет: обслуживать некому
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "stalling-fake",
                            "version": "1",
                        },
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "result": {"tools": []},
                }
            )
        elif msg.get("id") is not None:
            send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})


if __name__ == "__main__":
    main()
