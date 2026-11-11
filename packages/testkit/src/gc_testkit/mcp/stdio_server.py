"""Тестовый MCP-сервер stdio на голом JSON-RPC (только стандартная библиотека).

Поведение — JSON-файл, путь к которому в ``GC_TEST_CONTROL``. Файл
перечитывается при старте и перед каждым ответом, поэтому тест меняет
поведение, не меняя определения сервера. Ключи:

* ``fail_start`` — выйти с кодом 1 до ответа на ``initialize``;
* ``start_delay`` — секунды до ответа на ``initialize``;
* ``tools`` — имена тулов ``tools/list`` (по умолчанию ``echo``, ``nap``);
  пустой список допустим;
* ``no_tools_key`` — ответ ``tools/list`` без ключа ``tools``;
* ``notifications`` — число уведомлений перед каждым ответом;
* ``noise`` — строки не-JSON перед каждым ответом (то есть и между ними);
* ``exit_on`` — получив запрос этого метода, выйти с кодом 1, не ответив;
* ``ignore_eof`` — не выходить по концу stdin;
* ``ignore_term`` — не выходить по SIGTERM;
* ``spawn_child`` — потомок ``sleep 300`` в той же группе;
* ``child_ignores_term`` — такой же потомок, глухой к SIGTERM (сам сервер по
  SIGTERM выходит) — форма ``node`` + Chromium.

Тулы: ``echo(text)`` → ``{"text", "pid", "trace"}`` (``trace`` — из
``TRACE_ID``), ``nap(seconds)`` → ``{"pid"}``, прерывается
``notifications/cancelled`` со своим id и тогда не отвечает; прочие имена
списка → ``{"pid"}``. Служебный ``crash()`` есть всегда, в список не входит:
выход с кодом 3 без ответа. Вызов имени вне списка — ошибка JSON-RPC.
Результат — ``{"content": [{"type": "text", "text": <JSON>}]}``, ответ на
``initialize`` — в форме клиента SDK ``mcp``: сервер обслуживает и ``stdio``,
и ``stream_stdio``.

stdin читает отдельный поток, каждый запрос исполняется в своём: пока идёт
``nap``, следующие запросы обслуживаются.

Журнал ``<control>.mcp-journal``: ``start <pid>``, ``child <pid>``,
``exit <pid>``, ``req <method> <id>``, ``cancelled <id>``. ``exit`` пишется при штатном
выходе — по концу stdin и по SIGTERM; аварийные (``fail_start``, ``exit_on``,
``crash``) его не пишут.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

PID = os.getpid()
CONTROL = os.environ.get("GC_TEST_CONTROL", "")
JOURNAL = CONTROL + ".mcp-journal"  # gc_testkit.mcp.JOURNAL_SUFFIX
DEFAULT_TOOLS = ["echo", "nap"]
SCHEMAS = {
    "echo": {"text": {"type": "string"}},
    "nap": {"seconds": {"type": "number"}},
}
NOISE = ("это не JSON", '{"jsonrpc": "2.0", "id"')
UNKNOWN_TOOL = -32602

_out_lock = threading.Lock()
_exit_lock = threading.Lock()
_cancels_lock = threading.Lock()
_cancels: dict = {}
_children: list = []


def behavior() -> dict:
    try:
        with open(CONTROL, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {}


def log(line: str) -> None:
    # Одна запись O_APPEND на строку: потоки и обработчик сигнала пишут без
    # общего лока, и обработчик не встанет в лок, прерванный им же.
    fd = os.open(JOURNAL, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def write(text: str) -> None:
    with _out_lock:
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except OSError:
            pass


def send(message: dict) -> None:
    write(json.dumps(message, ensure_ascii=False))


def exit_logged() -> None:
    """Штатный выход ровно с одной строкой ``exit``.

    Конец stdin и SIGTERM приходят почти одновременно при закрытии сессии;
    второй, увидев занятый лок, уступает первому.
    """
    if _exit_lock.acquire(blocking=False):
        log(f"exit {PID}")
        os._exit(0)


def on_term(_signum, _frame) -> None:
    exit_logged()


def spawn_child(ignores_term: bool) -> None:
    # Игнорирование сигнала переживает exec, обработчик — нет: на время
    # запуска ставим то, что должен унаследовать потомок.
    disposition = signal.SIG_IGN if ignores_term else signal.SIG_DFL
    previous = signal.signal(signal.SIGTERM, disposition)
    try:
        # Вывод потомка — не наш stdout: иначе клиент не увидит конца
        # вывода, когда лидер умрёт.
        child = subprocess.Popen(
            ["sleep", "300"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
    _children.append(child)
    log(f"child {child.pid}")


def ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def tools_of(cfg: dict) -> list:
    return list(cfg.get("tools", DEFAULT_TOOLS))


def describe(name: str) -> dict:
    return {
        "name": name,
        "description": f"тестовый тул {name}",
        "inputSchema": {
            "type": "object",
            "properties": SCHEMAS.get(name, {}),
        },
    }


def run_tool(name: str, arguments: dict, cancel: threading.Event):
    if name == "echo":
        return {
            "text": arguments.get("text", ""),
            "pid": PID,
            "trace": os.environ.get("TRACE_ID", ""),
        }
    if name == "nap" and cancel.wait(float(arguments.get("seconds", 0))):
        return None
    return {"pid": PID}


def call(rid, params: dict, cfg: dict, cancel: threading.Event):
    name = params.get("name")
    if name == "crash":
        os._exit(3)
    if name not in tools_of(cfg):
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": UNKNOWN_TOOL, "message": f"unknown tool {name}"},
        }
    out = run_tool(name, params.get("arguments") or {}, cancel)
    if out is None:
        return None
    text = json.dumps(out, ensure_ascii=False)
    return ok(rid, {"content": [{"type": "text", "text": text}]})


def initialize(rid, params: dict, cfg: dict) -> dict:
    time.sleep(float(cfg.get("start_delay", 0)))
    return ok(
        rid,
        {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "gc-test-stdio", "version": "1"},
        },
    )


def answer(message: dict, cfg: dict, cancel: threading.Event):
    rid = message["id"]
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return initialize(rid, params, cfg)
    if method == "tools/list":
        if cfg.get("no_tools_key"):
            return ok(rid, {})
        return ok(rid, {"tools": [describe(n) for n in tools_of(cfg)]})
    if method == "tools/call":
        return call(rid, params, cfg, cancel)
    return ok(rid, {})


def preamble(cfg: dict) -> None:
    for _ in range(int(cfg.get("notifications", 0))):
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "тестовое уведомление"},
            }
        )
    if cfg.get("noise"):
        for line in NOISE:
            write(line)


def handle(message: dict, cancel: threading.Event) -> None:
    cfg = behavior()
    if cfg.get("exit_on") == message.get("method"):
        os._exit(1)
    try:
        reply = answer(message, cfg, cancel)
    finally:
        with _cancels_lock:
            _cancels.pop(message["id"], None)
    if reply is not None:
        preamble(cfg)
        send(reply)


def cancel_request(rid) -> None:
    log(f"cancelled {rid}")
    with _cancels_lock:
        event = _cancels.get(rid)
    if event is not None:
        event.set()


def dispatch(raw: str) -> None:
    try:
        message = json.loads(raw)
    except ValueError:
        return
    if not isinstance(message, dict) or message.get("method") is None:
        return
    if "id" not in message:
        if message["method"] == "notifications/cancelled":
            cancel_request((message.get("params") or {}).get("requestId"))
        return
    log(f"req {message['method']} {message['id']}")
    cancel = threading.Event()
    with _cancels_lock:
        _cancels[message["id"]] = cancel
    threading.Thread(
        target=handle, args=(message, cancel), daemon=True
    ).start()


def read_stdin() -> None:
    for raw in sys.stdin:
        dispatch(raw)
    if not behavior().get("ignore_eof"):
        exit_logged()


def main() -> None:
    if not CONTROL:
        sys.exit("GC_TEST_CONTROL не задан: журнал писать некуда")
    cfg = behavior()
    term = signal.SIG_IGN if cfg.get("ignore_term") else on_term
    signal.signal(signal.SIGTERM, term)
    log(f"start {PID}")
    if cfg.get("fail_start"):
        os._exit(1)
    if cfg.get("spawn_child") or cfg.get("child_ignores_term"):
        spawn_child(bool(cfg.get("child_ignores_term")))
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
