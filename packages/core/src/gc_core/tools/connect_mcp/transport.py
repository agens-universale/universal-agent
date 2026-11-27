"""
Транспортный слой MCP: один запрос — один ответ (синхронно).
Тип транспорта задаётся в конфиге сервера (transport: http | sse | stdio).
"""

import asyncio
import contextlib
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

import httpx

from gc_core.tools.connect_mcp.call_errors import (
    McpCallOutcomeUnknown,
    McpCallTimedOut,
    McpServerGone,
)
from gc_core.utils.ssl_config import get_client_cert, get_verify_ssl

_SSE_DATA_PREFIX = "data:"


def _normalize_url(url: str) -> str:
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"http://{url}"
    return url


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


def _parse_http_response(
    resp: "httpx.Response",
) -> Tuple[Dict[str, str], Dict[str, Any]]:
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
        parsed = _try_parse_json_rpc(line[len(_SSE_DATA_PREFIX) :].strip())
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
        _normalize_url(url),
        json=json_payload,
        headers=_http_headers_for(headers),
        timeout=timeout,
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
            print(
                f"[MCP_DEBUG SSE] {line[:200]!r}"
                + ("..." if len(line) > 200 else "")
            )
        stripped = line.strip()
        if not stripped:
            return self._on_blank_line()
        if stripped.lower().startswith(_SSE_DATA_PREFIX):
            return self._on_data_line(
                stripped[len(_SSE_DATA_PREFIX) :].strip()
            )
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
    # httpx типизирует Response.encoding как `str | None`, хотя сам подставляет
    # utf-8, если сервер кодировку не прислал. Проверка на None — страховка от
    # смены этого поведения в httpx, а не мёртвая ветка; NOSONAR — на её строке.
    if resp.encoding is None or resp.encoding.lower() in (
        "iso-8859-1",
        "ascii",
    ):  # NOSONAR: S3403
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
        "POST",
        _normalize_url(url),
        json=json_payload,
        headers=_sse_headers_for(headers),
        timeout=timeout,
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


def _cancelled_notification(request_id: Any) -> Dict[str, Any]:
    """Уведомление MCP, которым клиент снимает уже отправленный запрос.

    Для stdio это и есть сигнал отмены: отдельного потока на вызов нет.
    ``initialize`` сюда не передавать — спека запрещает отменять его.
    """
    return {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {
            "requestId": request_id,
            "reason": "User requested cancellation",
        },
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

    __slots__ = ("_ended", "_queue", "_stream", "_thread")

    def __init__(self, stream: "Any") -> None:
        self._stream = stream
        self._ended = False
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
                text = (
                    line
                    if isinstance(line, str)
                    else line.decode("utf-8", "replace")
                )
                self._queue.put(text)
        except Exception:
            pass
        finally:
            # None — маркер «поток кончился»: без него читатель ждал бы таймаут
            # на каждой строке после смерти процесса. Маркер один, поэтому
            # чтения после него смотрят на ``_ended``.
            self._ended = True
            self._queue.put(None)

    @property
    def ended(self) -> bool:
        """Поток вывода кончился: процесс закрыл stdout или умер."""
        return self._ended

    def readline(self, timeout: float) -> "str | None":
        import time as _time

        from gc_core.user_stop import UserStopInterrupt, current_user_stop

        deadline = _time.monotonic() + timeout
        while True:
            stop = current_user_stop()
            if stop is not None and stop.requested:
                stop.notify_observed()
                raise UserStopInterrupt(kind="mcp", started=True)
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return None
            try:
                return self._queue.get(timeout=min(0.02, remaining))
            except queue.Empty:
                if self._ended:
                    return self._rest_after_end()

    def _rest_after_end(self) -> "str | None":
        """Конец потока увиден после пустой очереди.

        Качальщик мог положить последнюю строку уже после того, как
        очередь оказалась пустой, и только потом поставить флаг конца:
        забираем её без ожидания, «конец» — только при пустой очереди.
        """
        try:
            return self._queue.get_nowait()
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

        from gc_core.user_stop import UserStopInterrupt, current_user_stop

        deadline = _time.monotonic() + timeout
        while True:
            stop = current_user_stop()
            if stop is not None and stop.requested:
                stop.notify_observed()
                raise UserStopInterrupt(kind="mcp", started=True)
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                if self._ended:
                    return self._rest_after_end()
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


async def _areadline_with_timeout(
    stream: "Any", timeout: float
) -> "str | None":
    """То же, не занимая поток на ожидании."""
    reader = _reader_for(stream)
    return None if reader is None else await reader.areadline(timeout)


def stdio_stream_ended(stream: "Any") -> bool:
    """Читатель этого потока видел его конец.

    Пустое чтение отсюда отличает смерть сервера от истёкшего срока: конец
    потока приходит раньше, чем процесс становится зомби, и мгновенный
    ``poll()`` в этот момент ещё видит процесс живым.
    """
    reader = _READERS.get(id(stream))
    return reader is not None and reader.ended


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


#: Команда сервера, которую запускает пускатель журнала процессов: в
#: ``command`` тогда интерпретатор пускателя, а каталог первым в PATH
#: (``mcp_process_env``) нужен каталог команды сервера.
LAUNCHED_COMMAND_FIELD = "_gc_launched_command"


def _stdio_command(
    server_cfg: Dict[str, Any],
) -> Tuple[List[str], Any, Dict[str, str]]:
    """Разобрать конфиг stdio-сервера в ``(argv, cwd, env)``.

    Общее для обоих способов запуска — синхронного ``Popen`` (долгоживущая
    сессия) и ``create_subprocess_exec`` (одноразовый обмен). Второй копии
    разбора быть не должно: расхождение здесь означало бы, что один и тот же
    сервер стартует с разным окружением. Под пускателем окружение строится
    по команде сервера (``LAUNCHED_COMMAND_FIELD``).
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

    from gc_core.mcp_runtime.env import mcp_process_env

    env_command = server_cfg.get(LAUNCHED_COMMAND_FIELD) or command
    return cmd, cwd, mcp_process_env(env_command, server_cfg.get("env"))


# --- stderr stdio-сервера ----------------------------------------------------
#
# Раньше stderr уходил в ``DEVNULL``, и сервер, упавший при старте
# (``ModuleNotFoundError`` в его venv), выглядел на экране ``/mcp`` как
# «нет соединения с ddgs-mcp» — без единого слова о причине. Теперь stderr
# пишется в отдельный приватный лог запуска. Фоновый читатель непрерывно
# вычитывает stderr; файл ограничен 1 МиБ, при переполнении остаётся хвост.
# Параллельные запуски не обнуляют логи друг друга; закрытых логов не более 32.

#: Сколько хвоста лога читать в поисках последней строки: причина падения —
#: в конце, а лог долгоживущего сервера может быть большим.
_STDERR_TAIL_BYTES = 64 * 1024
#: Потолок строки причины: на экране ``/mcp`` она идёт одной строкой, как
#: прочие причины дискавери (~120 знаков); путь лога добавляется целиком.
_STDERR_LINE_MAX = 120
#: Сколько ждать, пока процесс без ответа на ``initialize`` завершится. EOF
#: stdout приходит раньше, чем выставляется код выхода.
_STARTUP_EXIT_GRACE_SEC = 1.0


class McpServerExitedError(ConnectionError):
    """stdio-сервер завершился, не ответив на ``initialize``.

    Наследник ``ConnectionError``: для всех, кто ловил обрыв трубы, это
    по-прежнему «нет соединения», меняется только текст причины.
    """

    def __init__(self, last_line: str, log_path: Path) -> None:
        self.last_line = last_line
        self.log_path = log_path
        super().__init__(
            f"сервер завершился при старте: {last_line} (лог: {log_path})"
        )


#: Потолок имени файла лога: имя сервера приходит из конфига как есть.
_LOG_NAME_MAX = 100


def _stderr_log_path(
    server_cfg: Dict[str, Any], server_name: Optional[str] = None
) -> Path:
    """Лог stderr: ``<state>/mcp/<имя>.<pid>.<запуск>.stderr.log``.

    Имя сервера (ключ в ``mcp-servers``) передают вниз дискавери и вызов
    тула: по команде два сервера с одной командой и разным env/cwd делили
    бы один файл. Без имени (прямой вызов транспорта) — как раньше,
    ``<команда>[-<хэш args>]``: хэш разводит хотя бы разные аргументы.
    Имя санитизируется для ФС: всё, кроме букв, цифр, ``.``/``_``/``-``,
    становится ``_`` — разделителю каталога в имени не бывать.
    """
    from gc_core.config_modules.env_vars import gc_paths

    name = _sanitize_log_name(server_name) if server_name else ""
    if not name:
        name = _sanitize_log_name(_command_log_name(server_cfg))
    from uuid import uuid4

    return (
        gc_paths()["state"]
        / "mcp"
        / f"{name}.{os.getpid()}.{uuid4().hex[:12]}.stderr.log"
    )


def _command_log_name(server_cfg: Dict[str, Any]) -> str:
    """``<команда>[-<хэш args>]`` — имя лога, когда имени сервера нет."""
    command = str(server_cfg.get("command") or "server")
    name = Path(command).name or "server"
    args = server_cfg.get("args")
    if args:
        argv = json.dumps(
            [str(a) for a in args] if isinstance(args, list) else [str(args)]
        )
        digest = hashlib.sha1(argv.encode("utf-8"), usedforsecurity=False)
        name = f"{name}-{digest.hexdigest()[:8]}"
    return name


def _sanitize_log_name(name: str) -> str:
    """Имя для файла в каталоге логов: без разделителей и прочего мусора."""
    return re.sub(r"[^\w.-]", "_", str(name))[:_LOG_NAME_MAX]


@contextlib.contextmanager
def _stderr_log(
    server_cfg: Dict[str, Any], server_name: Optional[str] = None
) -> Iterator[Tuple[Any, Optional[Path]]]:
    """``(куда писать stderr, путь лога)`` на время запуска процесса.

    Файл отдельный для каждого запуска, ограничен 1 МиБ; pipe постоянно
    вычитывается фоновым потоком. Старые закрытые логи ограничены по числу.
    Лог не открылся (каталог только на чтение) — ``DEVNULL`` и пути нет:
    диагностика не повод не запустить сервер.
    """
    path = _stderr_log_path(server_cfg, server_name)
    from gc_core.tools.connect_mcp.stderr_capture import capture

    with contextlib.ExitStack() as stack:
        try:
            result = stack.enter_context(capture(path, _open_private_log))
        except OSError:
            result = subprocess.DEVNULL, None
        yield result


def _open_private_log(path: Path) -> Any:
    """Открыть лог на запись с нуля — только для владельца.

    Каждый запуск получает уникальный путь. ``O_APPEND`` позволяет после
    обрезки большого лога продолжать запись с его текущего конца.

    В stderr серверы печатают ошибки авторизации вместе с токенами, поэтому
    файл 0600, а каталог создаётся 0700 (чужой существующий не трогаем).
    ``O_NOFOLLOW`` — не писать по подложенному симлинку; на Windows его нет,
    и права там почти не значат, но открытие работает так же. Лог, оставшийся
    с более широкими правами, ``fchmod`` сужает: ``O_CREAT`` их не меняет.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _last_stderr_line(path: Path) -> str:
    """Последняя непустая строка лога, секреты замаскированы; пусто — нет."""
    from gc_core.tools.connect_mcp.stderr_capture import finish

    finish(path)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - _STDERR_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for raw in reversed(tail.splitlines()):
        # NUL — след дыры в логе (старый лог, второй писатель): ``strip()``
        # их не снимает, а в строку ``/mcp`` им незачем.
        line = raw.replace("\x00", "").strip()
        if line:
            # Строка уходит в UI (``reason`` на экране ``/mcp``) — секреты
            # маскируем общим редактором логов, и до обрезки: обрезанный
            # токен паттерн уже не узнал бы.
            from gc_core.security.log_redaction import Redactor

            return Redactor().redact(line)[:_STDERR_LINE_MAX]
    return ""


def _exit_error_from_log(
    exited: bool, log_path: Optional[Path]
) -> Optional[McpServerExitedError]:
    """Ошибка «сервер завершился при старте», если её есть чем подкрепить.

    Общая часть всех stdio-путей: асинхронного и блокирующего ожиданий
    выхода (одноразовый обмен, долгоживущая сессия) и SDK-транспорта
    ``stream_stdio``, где процесса не видно и «умер» — это закрытое
    соединение до ``initialize``; имя нарочно не ``_startup_exit_error``:
    рядом с ``_astartup_exit_error`` это был бы новый sync/async-двойник
    (``tests/test_sync_async_pairs.py``), а тут не двойник, а общее ядро.

    Процесс жив (``exited`` ложно) или stderr пуст — ``None``, и
    вызывающий оставляет прежнее поведение.
    """
    if not exited or log_path is None:
        return None
    line = _last_stderr_line(log_path)
    return McpServerExitedError(line, log_path) if line else None


async def _astartup_exit_error(
    proc: Any, log_path: Optional[Path]
) -> Optional[McpServerExitedError]:
    """Асинхронная форма для ``asyncio``-подпроцесса одноразового обмена."""
    deadline = time.monotonic() + _STARTUP_EXIT_GRACE_SEC
    while proc.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return await asyncio.to_thread(
        _exit_error_from_log, proc.returncode is not None, log_path
    )


def _wait_startup_exit_error(
    proc: "subprocess.Popen", log_path: Optional[Path]
) -> Optional[McpServerExitedError]:
    """Синхронная форма для ``Popen`` долгоживущей сессии."""
    try:
        proc.wait(timeout=_STARTUP_EXIT_GRACE_SEC)
    except subprocess.TimeoutExpired:
        return None
    return _exit_error_from_log(proc.returncode is not None, log_path)


# Потолок строки JSON-RPC у asyncio-подпроцесса. Дефолт ``StreamReader`` —
# 64 КиБ, и на строке длиннее ``readline()`` бросает ``ValueError: Separator
# is not found, and chunk exceed the limit``, теряя буфер. Через этот порог
# перешагивает обычный ``tools/list`` сервера на пару сотен инструментов и
# любой сколько-нибудь крупный результат ``tools/call``; на ``Popen(text=True)``,
# откуда транспорт переезжал, потолка не было вовсе. 32 МиБ — это про
# «не съесть память на мусорном потоке», а не про формат ответа.
_STDIO_READ_LIMIT = 32 * 1024 * 1024


async def _aspawn_stdio_process(server_cfg: Dict[str, Any], stderr: Any):
    """Запустить процесс MCP-сервера на текущем loop.

    Годится только для ОДНОРАЗОВОГО обмена: asyncio-подпроцесс привязан к
    своему loop, и на чужом падает с RuntimeError (проверено). Долгоживущая
    сессия поэтому осталась на ``subprocess.Popen`` — см. ``persistent_stdio``.
    ``stderr`` — из ``_stderr_log``.
    """
    cmd, cwd, env = _stdio_command(server_cfg)
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        cwd=cwd,
        env=env,
        limit=_STDIO_READ_LIMIT,
    )


async def _aread_response(
    proc, timeout: float, want_id: Any
) -> Dict[str, Any]:
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

    Ответа нет — ошибка, а не ``{}``: пустой ответ модель приняла бы за
    успех. Конец вывода — ``McpServerGone``, срок — ``McpCallTimedOut``.
    """
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise McpCallTimedOut(timeout)
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), left)
        except TimeoutError:
            raise McpCallTimedOut(timeout) from None
        if not raw:
            raise McpServerGone()  # EOF: процесс закрыл вывод
        line_in = raw.decode("utf-8", "replace").strip()
        message = (
            _parse_rpc_response_line(line_in, want_id) if line_in else None
        )
        if message is not None:
            return message


def _parse_rpc_response_line(
    line_in: str, want_id: Any
) -> Dict[str, Any] | None:
    """JSON-RPC ответ на наш запрос из строки stdout; None — мусор, встречный запрос или чужой id."""
    try:
        message = json.loads(line_in)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict) or message.get("method") is not None:
        return None  # встречный запрос или уведомление
    if want_id is not None and message.get("id") != want_id:
        return None  # ответ на чужой запрос
    return message


async def _aread_step(
    proc, timeout: float, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Ответ на шаг обмена; сбой рукопожатия — ошибка соединения.

    «Исход неизвестен» значит «запрос мог исполниться», а на ``initialize``
    сам запрос ещё не уходил: такой сбой повторяется, как любой обрыв.
    """
    try:
        return await _aread_response(proc, timeout, payload.get("id"))
    except McpCallOutcomeUnknown as exc:
        if payload.get("method") != "initialize":
            raise
        raise ConnectionError(f"initialize: {exc}") from exc


async def _astdio_exchange(
    server_cfg: Dict[str, Any],
    timeout: float,
    steps: List[Tuple[Dict[str, Any], bool]],
    server_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Один процесс на обмен, без потоков.

    Таймаут чтения здесь нативный — ``asyncio.wait_for`` вокруг
    ``StreamReader.readline()``. Раньше на каждую строку JSON-RPC заводился
    поток (``_LineReader``): прервать блокирующий ``readline`` на pipe иначе
    было нечем, и ``selectors`` на Windows с pipe не работает. Корутину
    прервать можно, поэтому поток на этом пути больше не нужен.

    ``server_name`` — только для имени лога stderr (``_stderr_log_path``).
    """
    with _stderr_log(server_cfg, server_name) as (stderr, log_path):
        proc = await _aspawn_stdio_process(server_cfg, stderr)
    results: List[Dict[str, Any]] = []
    try:
        try:
            await _aexchange_steps(proc, timeout, steps, results)
        except OSError as exc:
            # Обрыв трубы до ответа на initialize — чаще всего сервер упал
            # при старте. Причина в его stderr, а не в «нет соединения».
            await _araise_if_exited_at_start(proc, log_path, results, exc)
            raise
        # Пустой ответ на initialize от умершего процесса раньше молча
        # становился «сессией без тулов».
        await _araise_if_exited_at_start(proc, log_path, results)
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


async def _aexchange_steps(
    proc: Any,
    timeout: float,
    steps: List[Tuple[Dict[str, Any], bool]],
    results: List[Dict[str, Any]],
) -> None:
    """Шаги обмена по порядку; ответы копятся в ``results`` по мере прихода."""
    for payload, expect_response in steps:
        proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        if not expect_response:
            results.append({})
            continue
        results.append(await _aread_step(proc, timeout, payload))


async def _araise_if_exited_at_start(
    proc: Any,
    log_path: Optional[Path],
    results: List[Dict[str, Any]],
    cause: Optional[BaseException] = None,
) -> None:
    """Бросить :class:`McpServerExitedError`, если сервер умер до initialize.

    Первый шаг каждого обмена — initialize, поэтому «до initialize» — это
    пустой или отсутствующий ``results[0]``. Сервер ответил, жив или молчал
    в stderr — ничего не делаем, вызывающий ведёт себя как раньше.
    """
    if results and results[0]:
        return
    exited = await _astartup_exit_error(proc, log_path)
    if exited is not None:
        raise exited from cause


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
    server_cfg: Dict[str, Any], server_name: Optional[str] = None
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
            responses = await _astdio_exchange(
                cfg, timeout, [(json_payload, True)], server_name
            )
            return (
                {"mcp-session-id": "stdio"},
                responses[0] if responses else {},
            )
        if method == "notifications/initialized":
            await _astdio_exchange(
                cfg,
                timeout,
                [(_INIT_PAYLOAD, True), (json_payload, False)],
                server_name,
            )
            return ({}, {})
        responses = await _astdio_exchange(
            cfg,
            timeout,
            [
                (_INIT_PAYLOAD, True),
                (_INITIALIZED_PAYLOAD, False),
                (json_payload, True),
            ],
            server_name,
        )
        return ({}, responses[2] if len(responses) > 2 else {})

    return arequest_fn


def aget_transport(
    server_cfg: Dict[str, Any], server_name: Optional[str] = None
) -> Callable[..., Any]:
    """Асинхронный двойник :func:`get_transport`.

    Та же единая сигнатура, только корутинная::

        await request(url, headers, json_payload, timeout) -> (headers, body)

    Легаси-транспорты (``http``/``sse``) ходят через ``httpx``, а у него есть
    ``AsyncClient`` — то есть нативная форма у них была всё это время, просто
    не использовалась, и порт клиента уводил их в поток.

    ``server_name`` нужен только stdio — для имени лога stderr сервера.
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
        return _arequest_stdio(server_cfg, server_name)

    raise ValueError(
        f"Invalid or missing 'transport' in MCP server config. Expected 'http', 'sse' or 'stdio', got: {transport!r}"
    )


__all__ = [
    "aget_transport",
]
