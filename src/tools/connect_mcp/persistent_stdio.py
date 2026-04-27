"""
Долгоживущая stdio MCP-сессия на весь цикл работы агента.

Стандартный stdio-транспорт спавнит отдельный процесс MCP-сервера на каждый
вызов tools/list и tools/call. Для серверов, которые внутри себя держат тяжёлый
ресурс (например, browser-use с Chromium), это приводит к запуску нового
браузера на каждый tool_call. Эта же схема убирает такую стоимость: процесс
запускается один раз, handshake выполняется один раз, все последующие вызовы
идут через живой stdin/stdout.

Активируется per-server флагом в config.yml:

    mcp-servers:
      browser:
        transport: stdio
        command: ...
        persistent_session: true

Жизненный цикл сессий привязан к AgentExecutor.run(): в finally вызывается
PersistentStdioRegistry.instance().close_all(), чтобы не оставлять висящие
Chromium между запусками агента.
"""

from typing import Any, Dict, Optional

import json
import os
import signal
import subprocess
import threading

from tools.connect_mcp.transport import (
    _INIT_PAYLOAD,
    _INITIALIZED_PAYLOAD,
    _readline_with_timeout,
    _resolve_command,
)


def _spawn_detached_stdio_process(server_cfg: Dict[str, Any]) -> subprocess.Popen:
    """
    Запуск stdio MCP-сервера как лидера собственной сессии/process group.

    Это принципиально для persistent-сессии: сервер может форкать тяжёлых
    детей (например, browser-use поднимает Chromium через Playwright).
    Если отправить SIGTERM только родителю, дети становятся orphan'ами
    и продолжают жить после завершения агента. Вывод целиком (parent + все
    дети) лечится только групповым сигналом, для которого нужен отдельный
    process group — его и создаём здесь.
    """
    command = server_cfg.get("command")
    if not command:
        raise ValueError("stdio MCP server config must have 'command'")
    command = _resolve_command(str(command))
    args = server_cfg.get("args") or []
    if not isinstance(args, list):
        args = [args]
    cmd = [command] + [str(a) for a in args]
    cwd = server_cfg.get("cwd")
    env_extra = server_cfg.get("env") or {}
    env = os.environ.copy()
    for k, v in env_extra.items():
        env[str(k)] = str(v)

    popen_kwargs: Dict[str, Any] = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        text=True,
        bufsize=1,
    )
    if os.name == "posix":
        # На POSIX это делает порождённый процесс session leader.
        popen_kwargs["start_new_session"] = True
    else:
        # На Windows — отдельный process group, чтобы CTRL_BREAK работал по всей группе.
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return subprocess.Popen(cmd, **popen_kwargs)


def _terminate_process_tree(proc: subprocess.Popen, graceful_timeout: float = 5.0) -> None:
    """Убить процесс вместе со всей его группой (детьми), с эскалацией до KILL."""
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        # Фоллбек на обычный terminate, если по какой-то причине
        # не смогли узнать pgid (например, процесс уже умер).
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=graceful_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    # Не успели завершиться по-хорошему — добиваем SIGKILL по всей группе.
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass


class PersistentStdioSession:
    """
    Одна долгоживущая stdio-сессия с MCP-сервером.

    Потокобезопасна: все операции ввода/вывода сериализуются одним lock'ом,
    что даёт корректную очередь запросов даже если агент-луп начнёт слать
    запросы из нескольких потоков.
    """

    def __init__(self, server_cfg: Dict[str, Any]) -> None:
        self._server_cfg = server_cfg
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        # id=1 зарезервирован за initialize; пользовательские запросы начинаются со 2.
        self._next_id = 2

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _ensure_started(self, timeout: float) -> None:
        if self._is_alive():
            return
        # На случай если процесс умер между вызовами — чистим хендл.
        if self._proc is not None:
            try:
                self._proc.wait(timeout=0.1)
            except Exception:
                pass
            self._proc = None

        self._proc = _spawn_detached_stdio_process(self._server_cfg)
        self._next_id = 2
        self._write_line(_INIT_PAYLOAD)
        # Ответ на initialize читаем, но его содержимое нам не нужно.
        self._read_matching(_INIT_PAYLOAD["id"], timeout)
        self._write_line(_INITIALIZED_PAYLOAD)

    def call(
        self,
        method: str,
        params: Dict[str, Any],
        timeout: float,
    ) -> Dict[str, Any]:
        with self._lock:
            self._ensure_started(timeout)
            req_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            self._write_line(payload)
            return self._read_matching(req_id, timeout)

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return
            # Некоторые MCP-серверы поднимают внешних, detached-потомков:
            # например browser-use на macOS запускает системный Google Chrome
            # (ppid=1, не в нашей process group), поэтому killpg до него не
            # дотянется. Сначала даём серверу шанс погасить их штатно через
            # собственный MCP-тул (например browser_close_all) — CDP-коннект
            # закроется корректно, Chrome завершится сам.
            close_tool = self._server_cfg.get("persistent_session_close_tool")
            if isinstance(close_tool, str) and close_tool.strip():
                try:
                    req_id = self._next_id
                    self._next_id += 1
                    self._write_line(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "method": "tools/call",
                            "params": {"name": close_tool.strip(), "arguments": {}},
                        }
                    )
                    # Короткий таймаут: если сервер не ответил — всё равно
                    # идём дальше и дожимаем SIGTERM/SIGKILL.
                    self._read_matching(req_id, 10.0)
                except Exception:
                    pass
            # Закрываем stdin: некоторые MCP-серверы по EOF корректно
            # завершают свой event loop.
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            _terminate_process_tree(proc, graceful_timeout=5.0)

    def _write_line(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("persistent stdio MCP process is not running")
        line = json.dumps(payload) + "\n"
        proc.stdin.write(line)
        proc.stdin.flush()

    def _read_matching(self, req_id: Any, timeout: float) -> Dict[str, Any]:
        """
        Прочитать JSON-RPC ответ с нужным id.
        Неcовпадающие/невалидные строки (например, уведомления сервера) пропускаем,
        но не больше небольшого числа итераций на один вызов, чтобы не зависнуть.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise RuntimeError("persistent stdio MCP process is not running")
        for _ in range(32):
            line = _readline_with_timeout(proc.stdout, timeout)
            if line is None:
                return {}
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            # notifications не имеют id — просто пропускаем.
            if "id" not in obj:
                continue
            if obj.get("id") == req_id:
                return obj
        return {}


class PersistentStdioRegistry:
    """Процессный реестр долгоживущих stdio-сессий, ключ — server_key из config.yml."""

    _instance: Optional["PersistentStdioRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._sessions: Dict[str, PersistentStdioSession] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> "PersistentStdioRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def get_or_create(
        self,
        server_key: str,
        server_cfg: Dict[str, Any],
    ) -> PersistentStdioSession:
        with self._lock:
            session = self._sessions.get(server_key)
            if session is None:
                session = PersistentStdioSession(server_cfg)
                self._sessions[server_key] = session
            return session

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass


def is_persistent_session(server_cfg: Dict[str, Any]) -> bool:
    """
    True, если в server_cfg включён флаг persistent_session.
    Применимо к любому транспорту (stdio, stream_stdio, stream_sse, stream_http).
    """
    if not isinstance(server_cfg, dict):
        return False
    return bool(server_cfg.get("persistent_session"))


def is_persistent_stdio(server_cfg: Dict[str, Any]) -> bool:
    """Specialized: persistent режим именно для classic stdio транспорта."""
    if not isinstance(server_cfg, dict):
        return False
    if server_cfg.get("transport") != "stdio":
        return False
    return bool(server_cfg.get("persistent_session"))


__all__ = [
    "PersistentStdioSession",
    "PersistentStdioRegistry",
    "is_persistent_stdio",
    "is_persistent_session",
]
