"""
Транспортный слой MCP: один запрос — один ответ (синхронно).
Тип транспорта задаётся в конфиге сервера (transport: http | sse | stdio).
"""
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import asyncio
import json
import subprocess
import queue
import threading
import time

import httpx

from gc_core.utils.aio_compat import run_coro_blocking
from gc_core.utils.ssl_config import get_verify_ssl, get_client_cert


def _normalize_url(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"http://{url}"
    return url


def _sync_client(
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
) -> httpx.Client:
    """Клиент на один обмен: у httpx TLS задаётся при создании, не на запросе.

    ``trust_env=False`` — тот же контракт, что у остального ядра: ``~/.netrc``
    не читаем (Basic-auth оттуда перетирает явный Authorization), прокси и CA
    передаём явно.
    """
    from gc_core.utils.async_http import build_ssl_context

    return httpx.Client(
        verify=build_ssl_context(verify, cert),
        trust_env=False,
        follow_redirects=True,
    )


def _request_http(
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    timeout: float,
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """
    Обычный HTTP: один POST, тело — JSON. Ответ — один JSON-объект.

    ``Accept`` обязателен: спека streamable HTTP требует, чтобы клиент был
    готов принять и JSON, и SSE — сервер вправе ответить любым из двух.
    Совместимая реализация без этого заголовка отвечает 406 Not Acceptable
    (проверено на FastMCP). Соседний ``_request_sse`` его ставил, а этот —
    нет, и транспорт ``http`` не работал ни с одним спек-совместимым сервером.
    """
    url = _normalize_url(url)
    headers = dict(headers)
    headers.setdefault("Accept", "application/json, text/event-stream")
    with _sync_client(verify, cert) as client:
        resp = client.post(url, json=json_payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    # Headers httpx -> обычный dict для единообразия
    out_headers = {k.lower(): v for k, v in resp.headers.items()}
    # Пустое тело (например, на notifications/initialized) — не парсить как JSON
    if not resp.content or not resp.content.strip():
        return out_headers, {}

    # Ответ может прийти В ДВУХ ФОРМАХ, и это не прихоть сервера: раз клиент
    # заявил, что принимает и JSON, и SSE, спека streamable HTTP разрешает
    # ответить любой. FastMCP на tools/list отвечает именно SSE-обёрткой
    # (`event: message` / `data: {…}`), и голый resp.json() на ней падает —
    # транспорт молча возвращал пустоту и «0 тулов» при успешном коннекте.
    content_type = (out_headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = "utf-8"
        for line in resp.text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            parsed = _try_parse_json_rpc(line[len("data:"):].strip())
            if parsed is not None:
                return out_headers, parsed
        return out_headers, {}

    try:
        return out_headers, resp.json()
    except (ValueError, json.JSONDecodeError):
        return out_headers, {}


def _try_parse_json_rpc(s: str) -> Dict[str, Any] | None:
    """Распарсить JSON; вернуть объект, если это JSON-RPC ответ с result/error, иначе None."""
    if not s or not s.strip():
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _read_sse_first_json(resp: "httpx.Response", timeout: float) -> Dict[str, Any]:
    """
    Читать стрим SSE построчно; вернуть первый JSON-RPC ответ (с result или error).
    Поддерживается многострочное событие: после "data:" следующие строки до пустой
    склеиваются. Парсим после каждой строки — сервер может не слать пустую строку после события.
    """
    buffer: list[str] = []
    in_data = False

    from gc_core.config_modules import env_vars
    debug = env_vars.env.mcp_debug
    # httpx.iter_lines отдаёт уже декодированные str — параметра decode_unicode
    # у него нет (это имя из requests).
    for line in resp.iter_lines():
        if line is None:
            continue
        if debug:
            print(f"[MCP_DEBUG SSE] {line[:200]!r}" + ("..." if len(line) > 200 else ""))
        stripped = line.strip()
        if not stripped:
            if in_data and buffer:
                data_str = "\n".join(buffer)
                parsed = _try_parse_json_rpc(data_str)
                if parsed is not None:
                    return parsed
                buffer = []
            in_data = False
            continue

        if stripped.lower().startswith("data:"):
            prefix_len = 5  # len("data:")
            part = stripped[prefix_len:].strip()
            if part:
                buffer.append(part)
                in_data = True
                data_str = "\n".join(buffer)
                parsed = _try_parse_json_rpc(data_str)
                if parsed is not None:
                    return parsed
            else:
                # empty "data:" line — reset only if not already accumulating
                if not in_data:
                    buffer = []
                in_data = True
        elif in_data and buffer:
            buffer.append(stripped)
            data_str = "\n".join(buffer)
            parsed = _try_parse_json_rpc(data_str)
            if parsed is not None:
                return parsed

    if in_data and buffer:
        parsed = _try_parse_json_rpc("\n".join(buffer))
        if parsed is not None:
            return parsed
    return {}


def _request_sse(
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    timeout: float,
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """
    SSE: POST с stream=True, Accept: text/event-stream.
    Читаем первое событие data: {...}, парсим JSON, возвращаем и закрываем соединение.
    """
    url = _normalize_url(url)
    headers = dict(headers)
    headers["Accept"] = "application/json, text/event-stream"

    # Стрим у httpx — контекстный менеджер: тело читается ВНУТРИ блока, за его
    # пределами соединение уже закрыто. Поэтому и разбор идёт внутри.
    with _sync_client(verify, cert) as client:
        with client.stream(
            "POST", url, json=json_payload, headers=headers, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            # SSE почти всегда UTF-8; без явного указания httpx может взять
            # заявленную сервером кодировку и сломать JSON с кириллицей.
            if resp.encoding is None or resp.encoding.lower() in (
                "iso-8859-1", "ascii",
            ):
                resp.encoding = "utf-8"
            out_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = _read_sse_first_json(resp, timeout)
    return out_headers, body


# --------- stdio transport: процесс на один обмен, без долгоживущих процессов ---------

_INIT_PAYLOAD: Dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        # Capabilities объявляем ПУСТЫМИ, и это принципиально.
        #
        # Раньше здесь стояло {"roots": {"listChanged": True}, "sampling": {}} —
        # обещание, которого этот клиент не выполняет. Он односторонний: пишет
        # запрос, читает ответ с нужным id, всё остальное пропускает. Обратные
        # запросы сервера обслуживать некому.
        #
        # Сервер, поверивший обещанию, шлёт нам roots/list и ЖДЁТ. Ответа нет,
        # и он выжидает свой таймаут целиком. Замер на @playwright/mcp:
        #   capabilities {}                     → первый вызов 1.1 с
        #   capabilities roots+sampling         → первый вызов 64.2 с
        #   capabilities только sampling        → первый вызов 0.7 с
        #   capabilities только roots           → первый вызов 61.2 с
        # Виноват именно roots: playwright по нему определяет рабочий каталог,
        # не дожидается, отваливается по таймауту и молча берёт cwd. Один раз
        # на сессию — поэтому симптом выглядел как «медленный старт браузера».
        #
        # Появится реализация roots/sampling — объявлять снова, не раньше.
        "capabilities": {},
        "clientInfo": {"name": "SurfaceClient", "version": "1.0.0"},
    },
}

_INITIALIZED_PAYLOAD: Dict[str, Any] = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


class _LineReader:
    """Один читающий поток на ПРОЦЕСС, а не на строку.

    Остался только для ДОЛГОЖИВУЩИХ stdio-сессий (``persistent_stdio``), где
    процесс делят разные loop и потому он на ``subprocess.Popen`` — там
    прервать блокирующий ``readline`` нечем, кроме отдельного потока.
    Одноразовый обмен переехал на ``_astdio_exchange``, и таймаут чтения там
    нативный: ``asyncio.wait_for`` вокруг ``StreamReader.readline()``.

    Прежняя реализация заводила поток на каждую прочитанную строку JSON-RPC —
    до 32 потоков на один вызов MCP-тула (``persistent_stdio`` читает в цикле,
    пропуская уведомления сервера). Хуже того, при таймауте такой поток
    оставался висеть на ``readline`` навсегда: join с таймаутом отпускает
    вызывающего, но не сам поток. Долгоживущий сервер накапливал их молча.

    Здесь поток один и живёт столько же, сколько процесс; строки складываются
    в очередь, а таймаут — это таймаут ожидания очереди.

    Почему вообще поток: ``readline`` на pipe нельзя прервать по таймауту
    кроссплатформенно — ``selectors`` не работает с pipe на Windows.
    """

    __slots__ = ("_queue", "_thread", "_stream")

    def __init__(self, stream: "Any") -> None:
        self._stream = stream
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._pump, daemon=True, name="mcp-stdio-reader"
        )
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in iter(self._stream.readline, ""):
                if line in (b"", None):
                    break
                text = line if isinstance(line, str) else line.decode("utf-8", "replace")
                self._queue.put(text)
        except Exception:
            pass
        finally:
            # None — маркер «поток кончился»: без него читатель ждал бы таймаут
            # на каждой строке после смерти процесса.
            self._queue.put(None)

    def readline(self, timeout: float) -> "str | None":
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


#: Читатели по объекту потока: у одного процесса один читатель, сколько бы раз
#: его ни просили прочитать строку. WeakKeyDictionary нельзя (файловые объекты
#: не всегда слабоссылаемы), поэтому чистим по факту закрытия процесса.
_READERS: "dict[int, _LineReader]" = {}


def _readline_with_timeout(stream: "Any", timeout: float) -> "str | None":
    """Прочитать одну строку из stream с таймаутом (кроссплатформенно)."""
    if stream is None:
        return None
    key = id(stream)
    reader = _READERS.get(key)
    if reader is None:
        reader = _LineReader(stream)
        _READERS[key] = reader
    return reader.readline(timeout)


def forget_stdio_reader(stream: "Any") -> None:
    """Забыть читателя закрытого процесса (зовётся при остановке сервера)."""
    if stream is not None:
        _READERS.pop(id(stream), None)


def _resolve_command(command: str) -> str:
    """
    Раскрывает `~`, ищет команду в mcp-env, на Windows резолвит PATHEXT.

    `subprocess.Popen` не делает tilde expansion (это работа шелла), и на
    Windows не учитывает PATHEXT (`npx` не найдёт `npx.cmd`/`.exe`). Делаем
    оба шага здесь — единственный чокпойнт перед спавном stdio MCP.
    """
    from gc_core.mcp_runtime.env import resolve_mcp_command

    return resolve_mcp_command(command)


def _stdio_command(server_cfg: Dict[str, Any]) -> Tuple[List[str], Any, Dict[str, str]]:
    """Разобрать конфиг stdio-сервера в ``(argv, cwd, env)``.

    Общее для обоих способов запуска — синхронного ``Popen`` (долгоживущая
    сессия) и ``create_subprocess_exec`` (одноразовый обмен). Второй копии
    разбора быть не должно: расхождение здесь означало бы, что один и тот же
    сервер стартует с разным окружением.
    """
    command = server_cfg.get("command")
    if not command:
        raise ValueError("stdio MCP server config must have 'command'")
    command = _resolve_command(str(command))
    args = server_cfg.get("args")
    if args is None:
        args = []
    if not isinstance(args, list):
        args = [args]
    cmd = [command] + [str(a) for a in args]
    cwd = server_cfg.get("cwd")

    from gc_core.mcp_runtime.env import augment_process_env

    env = augment_process_env()
    for k, v in (server_cfg.get("env") or {}).items():
        env[str(k)] = str(v)
    return cmd, cwd, env


def _spawn_stdio_process(server_cfg: Dict[str, Any]) -> subprocess.Popen:
    """Запустить процесс MCP-сервера по конфигу (command, args, cwd, env)."""
    cmd, cwd, env = _stdio_command(server_cfg)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )


async def _aspawn_stdio_process(server_cfg: Dict[str, Any]):
    """Запустить процесс MCP-сервера на текущем loop.

    Годится только для ОДНОРАЗОВОГО обмена: asyncio-подпроцесс привязан к
    своему loop, и на чужом падает с RuntimeError (проверено). Долгоживущая
    сессия поэтому осталась на ``subprocess.Popen`` — см. ``persistent_stdio``.
    """
    cmd, cwd, env = _stdio_command(server_cfg)
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=env,
    )


async def _astdio_exchange(
    server_cfg: Dict[str, Any],
    timeout: float,
    steps: List[Tuple[Dict[str, Any], bool]],
) -> List[Dict[str, Any]]:
    """Один процесс на обмен, без потоков.

    Таймаут чтения здесь нативный — ``asyncio.wait_for`` вокруг
    ``StreamReader.readline()``. Раньше на каждую строку JSON-RPC заводился
    поток (``_LineReader``): прервать блокирующий ``readline`` на pipe иначе
    было нечем, и ``selectors`` на Windows с pipe не работает. Корутину
    прервать можно, поэтому поток на этом пути больше не нужен.
    """
    proc = await _aspawn_stdio_process(server_cfg)
    results: List[Dict[str, Any]] = []
    try:
        for payload, expect_response in steps:
            proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            if not expect_response:
                results.append({})
                continue
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout)
            except (asyncio.TimeoutError, TimeoutError):
                results.append({})
                continue
            line_in = raw.decode("utf-8", "replace").strip()
            try:
                results.append(json.loads(line_in) if line_in else {})
            except json.JSONDecodeError:
                results.append({})
    finally:
        await _akill_process(proc)
    return results


async def _akill_process(proc) -> None:
    """Завершить процесс, не дожидаясь EOF его пайпов.

    ``Process.wait()`` резолвится только когда закрыты ВСЕ пайпы, а не когда
    процесс вышел: сервер, оставивший фонового потомка с унаследованным
    stdout, подвесил бы нас на полный таймаут. Ждём ``returncode`` — он
    выставляется по выходу процесса. Та же грабля, что была в
    ``backends/local.py``.
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    deadline = time.monotonic() + 5.0
    while proc.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    if proc.returncode is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _stdio_exchange(
    server_cfg: Dict[str, Any],
    timeout: float,
    steps: List[Tuple[Dict[str, Any], bool]],
) -> List[Dict[str, Any]]:
    """Синхронный двойник. Реализация одна — асинхронная."""
    return run_coro_blocking(_astdio_exchange(server_cfg, timeout, steps))


def _request_stdio(
    server_cfg: Dict[str, Any],
) -> Callable[..., Tuple[Dict[str, str], Dict[str, Any]]]:
    """
    Вернуть функцию запроса для stdio с той же сигнатурой, что и http/sse:
    (url, headers, json_payload, timeout) -> (response_headers, body_dict).
    Один вызов = один процесс: handshake при необходимости, один ответ, процесс завершается.
    """
    cfg = server_cfg

    def request_fn(
        url: str,
        headers: Dict[str, str],
        json_payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        method = (json_payload or {}).get("method")
        if method == "initialize":
            steps: List[Tuple[Dict[str, Any], bool]] = [(json_payload, True)]
            responses = _stdio_exchange(cfg, timeout, steps)
            body = responses[0] if responses else {}
            return ({"mcp-session-id": "stdio"}, body)
        if method == "notifications/initialized":
            steps = [(_INIT_PAYLOAD, True), (json_payload, False)]
            _stdio_exchange(cfg, timeout, steps)
            return ({}, {})
        # tools/list, tools/call и др.: полный handshake в одном процессе
        steps = [(_INIT_PAYLOAD, True), (_INITIALIZED_PAYLOAD, False), (json_payload, True)]
        responses = _stdio_exchange(cfg, timeout, steps)
        body = responses[2] if len(responses) > 2 else {}
        return ({}, body)

    return request_fn


def get_transport(server_cfg: Dict[str, Any]) -> Callable[..., Tuple[Dict[str, str], Dict[str, Any]]]:
    """
    Вернуть функцию запроса по конфигу сервера.

    server_cfg должен содержать ключ "transport" со значением "http", "sse" или "stdio".

    Возвращаемая функция имеет единую сигнатуру для всех транспортов:
        request(url, headers, json_payload, timeout) -> (response_headers, body_dict)
    """
    transport = (server_cfg or {}).get("transport")

    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)

    if transport == "http":
        def request_http(
            url: str,
            headers: Dict[str, str],
            json_payload: Dict[str, Any],
            timeout: float,
        ) -> Tuple[Dict[str, str], Dict[str, Any]]:
            return _request_http(url, headers, json_payload, timeout, verify=verify, cert=cert)

        return request_http

    if transport == "sse":
        def request_sse(
            url: str,
            headers: Dict[str, str],
            json_payload: Dict[str, Any],
            timeout: float,
        ) -> Tuple[Dict[str, str], Dict[str, Any]]:
            return _request_sse(url, headers, json_payload, timeout, verify=verify, cert=cert)

        return request_sse
    if transport == "stdio":
        return _request_stdio(server_cfg)
    raise ValueError(
        f"Invalid or missing 'transport' in MCP server config. "
        f"Expected 'http', 'sse' or 'stdio', got: {transport!r}"
    )


__all__ = [
    "get_transport",
    "_request_http",
    "_request_sse",
    "_request_stdio",
]
