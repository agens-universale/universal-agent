"""
Изолированный вызов MCP-тула (в т.ч. SSE-сервер).
Печатает все шаги и сырые ответы для отладки.

Запуск (из-за общего arg_parser нужен -q ""):
  python -m gc_core.tools.connect_mcp.test_playground.run_sse_tool -q ""

Прежние строки (`PYTHONPATH=src`, `-m tools.…`) относились к плоской раскладке и
после разбиения на пакеты ведут в никуда.

Конфиг: CONFIG_PATH=/path/to/config.yml, иначе config.yml в корне рабочей копии.
Из установленного колеса рабочей копии нет — тогда CONFIG_PATH обязателен.
"""
from __future__ import annotations

import json
import os
import sys

# Корень рабочей копии — по маркеру, а не счётом `..`. Прежние четыре уровня
# вверх давали корень репозитория, пока модуль лежал в `src/tools/…`; после
# переезда в `packages/core/src/gc_core/tools/…` та же арифметика указывает на
# `packages/core/src`, где никакого config.yml нет, и скрипт падал на open().
# Вне рабочей копии (запуск из установленного колеса) корня нет вовсе — тогда
# нужен CONFIG_PATH.
from pathlib import Path

from gc_core.bundled_assets import repo_root_from

_REPO = repo_root_from(Path(__file__))
_CONFIG_PATH = os.environ.get(
    "CONFIG_PATH",
    str(_REPO / "config.yml") if _REPO else "",
)
# sys.path трогать больше не нужно: модуль импортируется как часть пакета
# gc_core, и его зависимости резолвятся штатно.


def _load_config():
    import yaml
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    config = _load_config()
    servers_cfg = config.get("mcp-servers") or {}
    mcp_meta = config.get("mcp-meta") or {}
    timeout = float(mcp_meta.get("request_timeout_sec") or 30)

    # Выбрать SSE-сервер (search)
    server_key = None
    server_cfg = None
    for k, v in servers_cfg.items():
        if isinstance(v, dict) and v.get("transport") == "sse":
            server_key = k
            server_cfg = v
            break
    if not server_key or not server_cfg:
        print("В config нет MCP-сервера с transport: sse. Выход.")
        sys.exit(1)

    url = server_cfg.get("url")
    token = server_cfg.get("token")
    if not url:
        print(f"У сервера '{server_key}' нет url. Выход.")
        sys.exit(1)

    print(f"Config: {_CONFIG_PATH}")
    print(f"SSE-сервер: {server_key} @ {url}")
    print(f"Timeout: {timeout}s\n")

    # Импорты после добавления path
    from gc_core.tools.connect_mcp.client import (
        _init_mcp_session,
        _tools_list_request,
        _call_mcp_tool,
        _MCP_SESSIONS,
    )

    # Очистить кэш сессии для чистого прогона
    _MCP_SESSIONS.pop(server_key, None)

    # 1) initialize
    print("1. initialize ...")
    session_id = _init_mcp_session(url, server_cfg, token=token, timeout=timeout)
    if not session_id:
        print("   Ошибка: не получен mcp-session-id")
        sys.exit(2)
    print(f"   Session ID: {session_id[:20]}...\n")

    # 2) tools/list
    print("2. tools/list ...")
    tools = _tools_list_request(
        url, session_id=session_id, server_cfg=server_cfg, token=token, timeout=timeout
    )
    print(f"   Получено тулов: {len(tools)}")
    for t in tools:
        print(f"   - {t.get('name')}: {t.get('description', '')[:60]}...")
    print()

    # 3) tools/call — web_search
    tool_name = "web_search"
    arguments = {"query": "python MCP protocol"}
    print(f"3. tools/call name={tool_name!r} arguments={json.dumps(arguments)} ...")
    try:
        raw = _call_mcp_tool(
            url=url,
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
            server_cfg=server_cfg,
            token=token,
            timeout=timeout,
        )
        print("   Ответ (сырой):")
        print(json.dumps(raw, indent=2, ensure_ascii=False))
        if isinstance(raw, dict):
            result = raw.get("result", raw)
            if result:
                print("\n   result:")
                print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])
    except Exception as e:
        print(f"   Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(3)

    print("\nГотово.")


if __name__ == "__main__":
    main()
