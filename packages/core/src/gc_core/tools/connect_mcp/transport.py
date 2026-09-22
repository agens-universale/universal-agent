"""
Транспортный слой MCP: один запрос — один ответ (синхронно).
Тип транспорта задаётся в конфиге сервера (transport: http | sse | stdio).
"""
import asyncio
import json
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import httpx

from gc_core.utils.ssl_config import get_client_cert, get_verify_ssl

_SSE_DATA_PREFIX = "data:"


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


def _http_headers_for(headers: Dict[str, str]) -> Dict[str, str]:
    """Заголовки одного обмена. Общее для обеих форм.

    ``Accept`` обязателен: спека streamable HTTP требует, чтобы клиент был
    готов принять и JSON, и SSE — сервер вправе ответить любым из двух.
    Совместимая реализация без этого заголовка отвечает 406 Not Acceptable
    (проверено на FastMCP). Соседний ``_request_sse`` его ставил, а этот —
    нет, и транспорт ``http`` не работал ни с одним спек-совместимым сервером.
    """
    headers = dict(headers)
    headers.setdefault("Accept", "application/json, text/event-stream")
    return headers


def _parse_http_response(resp: "httpx.Response") -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Ответ обычного HTTP → ``(заголовки, тело)``. Общее для обеих форм.

    Тело может прийти В ДВУХ ФОРМАХ, и это не прихоть сервера: раз клиент
    заявил, что принимает и JSON, и SSE, спека streamable HTTP разрешает
    ответить любой. FastMCP на tools/list отвечает именно SSE-обёрткой
    (`event: message` / `data: {…}`), и голый ``resp.json()`` на ней падает —
    транспорт молча возвращал пустоту и «0 тулов» при успешном коннекте.
    """
    # Headers httpx -> обычный dict для единообразия
    out_headers = {k.lower(): v for k, v in resp.headers.items()}
    # Пустое тело (например, на notifications/initialized) — не парсить как JSON
    if not resp.content or not resp.content.strip():
        return out_headers, {}

    content_type = (out_headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        return out_headers, _first_json_rpc_from_sse_body(resp)

    try:
        return out_headers, resp.json()
    except ValueError:
        return out_headers, {}


def _first_json_rpc_from_sse_body(resp: "httpx.Response") -> Dict[str, Any]:
    """Первый JSON-RPC ответ из уже полученного (не потокового) SSE-тела; иначе {}."""
    _fix_sse_encoding(resp)
    for line in resp.text.splitlines():
        line = line.strip()
        if not line.startswith(_SSE_DATA_PREFIX):
            continue
        parsed = _try_parse_json_rpc(line[len(_SSE_DATA_PREFIX):].strip())
        if parsed is not None:
            return parsed
    return {}


async def _arequest_http(
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    timeout: float,
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Асинхронный двойник :func:`_request_http`.

    Отличается ровно клиентом и ожиданием: заголовки и разбор ответа общие.
    Клиент берётся из пер-loop реестра (``get_async_client``) — там уже решены
    привязка пула соединений к loop и ключевание по ``verify``/``cert``.
    """
    from gc_core.utils.async_http import get_async_client

    client = get_async_client(verify=verify, cert=cert)
    resp = await client.post(
        _normalize_url(url), json=json_payload,
        headers=_http_headers_for(headers), timeout=timeout,
    )
    resp.raise_for_status()
    return _parse_http_response(resp)


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


class _SseFirstJson:
    """Накопитель события SSE: строки → первый JSON-RPC ответ (result или error).

    Push-парсер: состояние в объекте, источник строк — не его забота. Ровно
    поэтому одна реализация обслуживает и синхронный ``iter_lines``, и
    асинхронный ``aiter_lines``; второй копии этого разбора быть не должно.

    Поддерживается многострочное событие: после ``data:`` следующие строки до
    пустой склеиваются. Парсим после КАЖДОЙ строки — сервер может не слать
    пустую строку после события.
    """

    def __init__(self, debug: bool = False) -> None:
        self._buffer: list[str] = []
        self._in_data = False
        self._debug = debug

    def feed(self, line: "str | None") -> "Dict[str, Any] | None":
        if line is None:
            return None
        if self._debug:
            print(f"[MCP_DEBUG SSE] {line[:200]!r}" + ("..." if len(line) > 200 else ""))
        stripped = line.strip()
        if not stripped:
            return self._on_blank_line()
        if stripped.lower().startswith(_SSE_DATA_PREFIX):
            return self._on_data_line(stripped[len(_SSE_DATA_PREFIX):].strip())
        if self._in_data and self._buffer:
            self._buffer.append(stripped)
            return self._parse_buffer()
        return None

    def _parse_buffer(self) -> "Dict[str, Any] | None":
        return _try_parse_json_rpc("\n".join(self._buffer))

    def _on_blank_line(self) -> "Dict[str, Any] | None":
        """Конец события: последняя попытка разбора, затем сброс состояния."""
        if self._in_data and self._buffer:
            parsed = self._parse_buffer()
            if parsed is not None:
                return parsed
            self._buffer = []
        self._in_data = False
        return None

    def _on_data_line(self, part: str) -> "Dict[str, Any] | None":
        if part:
            self._buffer.append(part)
            self._in_data = True
            return self._parse_buffer()
        if not self._in_data:
            self._buffer = []
        self._in_data = True
        return None

    def flush(self) -> "Dict[str, Any] | None":
        """Стрим кончился: последняя попытка разобрать накопленное."""
        if self._in_data and self._buffer:
            return _try_parse_json_rpc("\n".join(self._buffer))
        return None


def _sse_debug() -> bool:
    from gc_core.config_modules import env_vars

    return bool(env_vars.env.mcp_debug)


def _fix_sse_encoding(resp: "httpx.Response") -> None:
    """SSE почти всегда UTF-8; без явного указания httpx может взять заявленную
    сервером кодировку и сломать JSON с кириллицей."""
    if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = "utf-8"


def _sse_headers_for(headers: Dict[str, str]) -> Dict[str, str]:
    headers = dict(headers)
    headers["Accept"] = "application/json, text/event-stream"
    return headers


async def _aread_sse_first_json(resp: "httpx.Response") -> Dict[str, Any]:
    """Асинхронный двойник :func:`_read_sse_first_json`; разбор общий."""
    reader = _SseFirstJson(_sse_debug())
    async for line in resp.aiter_lines():
        parsed = reader.feed(line)
        if parsed is not None:
            return parsed
    return reader.flush() or {}


async def _arequest_sse(
    url: str,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    timeout: float,
    verify: Union[bool, str] = True,
    cert: Optional[tuple] = None,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Асинхронный двойник :func:`_request_sse`; заголовки и разбор общие."""
    from gc_core.utils.async_http import get_async_client

    client = get_async_client(verify=verify, cert=cert)
    async with client.stream(
        "POST", _normalize_url(url), json=json_payload,
        headers=_sse_headers_for(headers), timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        _fix_sse_encoding(resp)
        out_headers = {k.lower(): v for k, v in resp.headers.items()}
        body = await _aread_sse_first_json(resp)
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

    __slots__ = ("_queue", "_stream", "_thread")

    def __init__(self, stream: "Any") -> None:
        self._stream = stream
        self._queue: queue.Queue[str | None] = queue.Queue()
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

    async def areadline(self, timeout: float) -> "str | None":
        """То же, но ожиданием очереди не занимает ни поток, ни loop.

        ``queue.get(timeout=…)`` блокирует вызывающего, поэтому синхронный
        двойник годится только в потоке. Здесь — ``get_nowait`` плюс пауза,
        отдающая управление циклу: поток-качальщик (``_pump``) остаётся, он
        один на процесс и структурно необходим, а вот держать РЯДОМ ещё один
        поток только ради ожидания незачем.
        """
        import asyncio
        import time as _time

        deadline = _time.monotonic() + timeout
        while True:
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                pass
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.02, remaining))


#: Читатели по объекту потока: у одного процесса один читатель, сколько бы раз
#: его ни просили прочитать строку. WeakKeyDictionary нельзя (файловые объекты
#: не всегда слабоссылаемы), поэтому чистим по факту закрытия процесса.
_READERS: "dict[int, _LineReader]" = {}


def _reader_for(stream: "Any") -> "_LineReader | None":
    """Читатель этого потока; заводится при первом обращении."""
    if stream is None:
        return None
    key = id(stream)
    reader = _READERS.get(key)
    if reader is None:
        reader = _LineReader(stream)
        _READERS[key] = reader
    return reader


def _readline_with_timeout(stream: "Any", timeout: float) -> "str | None":
    """Прочитать одну строку из stream с таймаутом (кроссплатформенно)."""
    reader = _reader_for(stream)
    return None if reader is None else reader.readline(timeout)


async def _areadline_with_timeout(stream: "Any", timeout: float) -> "str | None":
    """То же, не занимая поток на ожидании."""
    reader = _reader_for(stream)
    return None if reader is None else await reader.areadline(timeout)


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


# Потолок строки JSON-RPC у asyncio-подпроцесса. Дефолт ``StreamReader`` —
# 64 КиБ, и на строке длиннее ``readline()`` бросает ``ValueError: Separator
# is not found, and chunk exceed the limit``, теряя буфер. Через этот порог
# перешагивает обычный ``tools/list`` сервера на пару сотен инструментов и
# любой сколько-нибудь крупный результат ``tools/call``; на ``Popen(text=True)``,
# откуда транспорт переезжал, потолка не было вовсе. 32 МиБ — это про
# «не съесть память на мусорном потоке», а не про формат ответа.
_STDIO_READ_LIMIT = 32 * 1024 * 1024


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
        limit=_STDIO_READ_LIMIT,
    )


async def _aread_response(proc, timeout: float, want_id: Any) -> Dict[str, Any]:
    """Прочитать ответ С НУЖНЫМ id, пропуская всё остальное.

    Раньше здесь читалась ОДНА строка и считалась ответом — что бы в ней ни
    лежало. Докстрока ``_INIT_PAYLOAD`` рядом утверждала обратное («читает
    ответ с нужным id, всё остальное пропускает»), и это расхождение было не
    теоретическим:

    * серверы шлют ``notifications/message`` (логи) НЕЗАПРОШЕННО, и такая
      строка становилась «ответом», а настоящий ответ оставался непрочитанным;
    * сервер, поверивший объявленной способности, шлёт встречный запрос
      (``roots/list``) — он тоже принимался за ответ.

    Пропускаем: уведомления (нет ``id``), встречные запросы сервера (есть
    ``method``) и ответы с чужим ``id``. Таймаут — общий на ожидание, а не на
    строку: болтливый сервер иначе растягивал бы его без предела.
    """
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return {}
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), left)
        except TimeoutError:
            return {}
        if not raw:
            return {}                     # EOF: процесс закрыл вывод
        line_in = raw.decode("utf-8", "replace").strip()
        message = _parse_rpc_response_line(line_in, want_id) if line_in else None
        if message is not None:
            return message


def _parse_rpc_response_line(line_in: str, want_id: Any) -> Dict[str, Any] | None:
    """JSON-RPC ответ на наш запрос из строки stdout; None — мусор, встречный запрос или чужой id."""
    try:
        message = json.loads(line_in)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict) or message.get("method") is not None:
        return None                       # встречный запрос или уведомление
    if want_id is not None and message.get("id") != want_id:
        return None                       # ответ на чужой запрос
    return message


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
            results.append(
                await _aread_response(proc, timeout, payload.get("id"))
            )
    finally:
        # Закрываем stdin ДО терминации: многие серверы по EOF корректно
        # завершают свой event loop сами, и тогда SIGTERM/SIGKILL не нужны
        # вовсе. Долгоживущая сессия так и делает (`_close_stdin`), а
        # одноразовый обмен — нет, хотя процесс тот же.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
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
    except OSError:
        return
    deadline = time.monotonic() + 5.0
    while proc.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    if proc.returncode is None:
        try:
            proc.kill()
        except OSError:
            pass


def _arequest_stdio(
    server_cfg: Dict[str, Any],
) -> Callable[..., Any]:
    """Асинхронный двойник :func:`_request_stdio`.

    Обмен по stdio уже асинхронный (``_astdio_exchange``), синхронная форма —
    шим над ним. Здесь вызывается async-форма напрямую: правила шагов
    (когда handshake, какой ответ брать) общие с синхронной, потому что это
    одни и те же три ветки по ``method``.
    """
    cfg = server_cfg

    async def arequest_fn(
        _url: str,
        _headers: Dict[str, str],
        json_payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        method = (json_payload or {}).get("method")
        if method == "initialize":
            responses = await _astdio_exchange(cfg, timeout, [(json_payload, True)])
            return ({"mcp-session-id": "stdio"}, responses[0] if responses else {})
        if method == "notifications/initialized":
            await _astdio_exchange(
                cfg, timeout, [(_INIT_PAYLOAD, True), (json_payload, False)]
            )
            return ({}, {})
        responses = await _astdio_exchange(
            cfg, timeout,
            [(_INIT_PAYLOAD, True), (_INITIALIZED_PAYLOAD, False), (json_payload, True)],
        )
        return ({}, responses[2] if len(responses) > 2 else {})

    return arequest_fn


def aget_transport(server_cfg: Dict[str, Any]) -> Callable[..., Any]:
    """Асинхронный двойник :func:`get_transport`.

    Та же единая сигнатура, только корутинная::

        await request(url, headers, json_payload, timeout) -> (headers, body)

    Легаси-транспорты (``http``/``sse``) ходят через ``httpx``, а у него есть
    ``AsyncClient`` — то есть нативная форма у них была всё это время, просто
    не использовалась, и порт клиента уводил их в поток.
    """
    transport = (server_cfg or {}).get("transport")

    verify = get_verify_ssl(server_cfg)
    cert = get_client_cert(server_cfg)

    if transport == "http":
        async def arequest_http(url, headers, json_payload, timeout):
            return await _arequest_http(
                url, headers, json_payload, timeout, verify=verify, cert=cert
            )

        return arequest_http

    if transport == "sse":
        async def arequest_sse(url, headers, json_payload, timeout):
            return await _arequest_sse(
                url, headers, json_payload, timeout, verify=verify, cert=cert
            )

        return arequest_sse

    if transport == "stdio":
        return _arequest_stdio(server_cfg)

    raise ValueError(
        f"Invalid or missing 'transport' in MCP server config. "
        f"Expected 'http', 'sse' or 'stdio', got: {transport!r}"
    )


__all__ = [
    "aget_transport",
]
