"""
Долгоживущая stdio MCP-сессия на весь цикл работы агента.

Стандартный stdio-транспорт спавнит отдельный процесс MCP-сервера на каждый
вызов tools/list и tools/call. Для серверов, которые внутри себя держат тяжёлый
ресурс (например, @playwright/mcp с Chromium), это приводит к запуску нового
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

Почему здесь ``subprocess.Popen``, а не ``asyncio.create_subprocess_exec``
=========================================================================

Это осознанное решение, а не недоделка переезда на asyncio (Задача 20 плана).

Asyncio-подпроцесс ПРИВЯЗАН к тому event loop, на котором создан: попытка
прочитать его stdout с другого loop даёт ``RuntimeError``. Проверено прямым
экспериментом — процесс, запущенный под ``asyncio.run`` №1, на ``asyncio.run``
№2 недоступен, тогда как ``Popen`` работает и из основного потока, и из-под
любого loop.

Сессия здесь ДОЛГОЖИВУЩАЯ и общая: её создаёт дискавери (в потоке из
abandonable-пула), зовут вызовы тулов (loop приложения),
а закрывает ``AgentExecutor.arun`` в своём finally. Привязка к одному loop
сломала бы её ровно так же, как ломались httpx-клиенты и MCP-сессии, пока их
не начали ключевать по ``id(loop)``. Для разделяемого между loop процесса
блокирующий ``Popen`` — правильный инструмент.

Одноразовый обмен — другое дело: он целиком укладывается в один вызов, и там
asyncio уместен и применён (``transport._astdio_exchange``), что заодно убрало
поток на каждую прочитанную строку.

Блокирующие вызовы отсюда не держат event loop: наверх они идут через
``run_abandonable``/``to_thread``, а GIL на ожидании pipe отпускается.
"""

from typing import Any, Dict, Optional

import asyncio
import json
import os
import signal
import subprocess
import threading
import time

from gc_core.mcp_session_owner import current_owner
from gc_core.utils.aio_compat import run_coro_blocking
from gc_core.tools.connect_mcp.transport import (
    _INIT_PAYLOAD,
    _INITIALIZED_PAYLOAD,
    _areadline_with_timeout,
    _readline_with_timeout,
    _resolve_command,
    forget_stdio_reader,
)

#: Общий бюджет на дочистку остатка сессий, когда прогон сняли снаружи.
#: Именно общий, а не на сессию: закрытие одной стоит до 7 с (SIGTERM →
#: таймаут → SIGKILL) плюс до 10 с на ответ close-тула, и умножать это на число
#: сессий значит держать уже снятый прогон десятки секунд.
_CANCEL_DRAIN_BUDGET_SEC = 3.0


def _spawn_detached_stdio_process(server_cfg: Dict[str, Any]) -> subprocess.Popen:
    """
    Запуск stdio MCP-сервера как лидера собственной сессии/process group.

    Это принципиально для persistent-сессии: сервер может форкать тяжёлых
    детей (например, @playwright/mcp поднимает Chromium).
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
    from gc_core.mcp_runtime.env import augment_process_env

    env = augment_process_env()
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


#: Шаг опроса при асинхронном ожидании смерти процесса. Компромисс: реже —
#: закрытие тянется дольше нужного, чаще — лишние пробуждения loop на пустом
#: месте. При graceful-таймауте 5 с это до сотни проверок, каждая — один
#: ``waitpid(WNOHANG)`` и один ``killpg(pgid, 0)``.
_POLL_STEP_SEC = 0.05


def _pgid_of(proc: subprocess.Popen) -> Optional[int]:
    """Группа сервера. ``None`` — групп в POSIX-смысле нет (Windows).

    Это ``proc.pid``, а не ``os.getpgid``: сервер запущен лидером своей
    сессии (``start_new_session``), и номер группы равен его pid. Спрашивать
    ``getpgid`` поздно — после смерти лидера он падает, а группа (его дети)
    в этот момент ещё жива и как раз нужна.
    """
    return proc.pid if os.name == "posix" else None


def _signal_group(pgid: int, sig: int) -> bool:
    """Сигнал группе. ``False`` — группы нет: в ней не осталось никого.

    ``pgid`` ≤ 1 отвергается сразу: ``killpg(0, …)`` бьёт СВОЮ группу, а
    ``killpg(-1, …)`` на macOS становится ``kill(1, …)``. У настоящего
    процесса такого pid не бывает — только у двойника в тесте.
    """
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _group_alive(pgid: int) -> bool:
    """Есть ли в группе ещё кто-нибудь, живой или не пожатый."""
    try:
        return _signal_group(pgid, 0)
    except PermissionError:
        return True                    # кто-то есть, но сигналить ему нам нельзя


def _signal_tree(proc: subprocess.Popen, pgid: Optional[int], *, kill: bool) -> bool:
    """Сигнал всему дереву сервера: группе, а без неё — хотя бы лидеру.

    ``False`` — бить некого: группы нет, лидер мёртв. Не бросает никогда:
    отказ сигнала не повод бросать снос на полпути, а обработчик прерывания в
    ``aclose`` подменил бы новым исключением исходное.
    """
    if pgid is not None:
        try:
            if _signal_group(pgid, signal.SIGKILL if kill else signal.SIGTERM):
                return True
        except OSError:
            pass                       # группа недоступна (EPERM) — остаётся лидер
    try:
        if proc.poll() is not None:
            return False
        # Лидер жив, а группы с его номером нет или она недоступна: он не
        # лидер своей группы (запущен в обход `_spawn_detached_stdio_process`).
        # Бьём хотя бы его.
        if kill:
            proc.kill()
        else:
            proc.terminate()
    except Exception:
        pass
    return True


async def _await_exit(
    proc: subprocess.Popen, timeout: float, pgid: Optional[int] = None
) -> bool:
    """Дождаться смерти процесса, а с ``pgid`` — и всей его группы.

    ``True`` — успел, ``False`` — таймаут. Замена ``proc.wait(timeout)``,
    которая не занимает ни поток, ни loop: ``poll()`` — неблокирующий
    ``waitpid(WNOHANG)``, ``killpg(pgid, 0)`` — мгновенный системный вызов,
    а пауза между проверками отдаёт управление циклу.

    Порядок проверок несущий: сначала лидер, потом группа. Лидер — наш прямой
    ребёнок, и пока ``poll()`` его не пожал, он зомби и числится в группе:
    без этого ``killpg`` не ответил бы «никого» никогда, и каждый снос стоил
    бы полный таймаут.
    """
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None and (pgid is None or not _group_alive(pgid)):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(min(_POLL_STEP_SEC, max(0.0, deadline - time.monotonic())))


async def _aterminate_process_tree(
    proc: subprocess.Popen, graceful_timeout: float = 5.0
) -> None:
    """Убить процесс вместе со всей его группой (детьми), с эскалацией до KILL.

    ЕДИНСТВЕННАЯ форма терминации. Синхронный близнец снят: он отличался
    ровно способом ждать — сигналы (``killpg``, ``terminate``) мгновенные
    системные вызовы и в потоке не нуждались никогда, блокировал только
    ``proc.wait(timeout)``. Держать ради него второй экземпляр эскалации
    дорого: разъехаться эти два экземпляра могли молча, а цена расхождения —
    выживший MCP-сервер. Синхронному вызывающему (``close`` — из ``atexit``
    или из ``finally`` встраивающего под живым loop) хватает
    ``run_coro_blocking`` — обмена с процессом здесь нет, только сигнал и
    ожидание, поэтому отдельная синхронная форма не нужна.

    Эскалация: SIGTERM по группе → graceful-таймаут → SIGKILL по группе.
    Терять её нельзя — сервер, игнорирующий SIGTERM (Chromium под
    @playwright/mcp), иначе переживёт агента.

    Ждём ГРУППУ, а не лидера. Лидер — сам MCP-сервер, а тяжёлое у него в
    детях: node под @playwright/mcp по SIGTERM выходит сразу, Chromium в той
    же группе SIGTERM игнорирует. Пока ожидание смотрело на один
    ``proc.poll()``, смерть лидера засчитывалась за смерть всех, и SIGKILL
    группе не уходил никогда; уже мёртвого лидера снос и вовсе пропускал, не
    тронув группу. Теперь группа мёртвого лидера получает тот же порядок:
    SIGTERM (послушная сирота уйдёт сразу, а не по таймауту), опрос
    ``killpg(pgid, 0)`` до «никого» в пределах того же graceful-таймаута,
    затем SIGKILL.

    Это только POSIX. На Windows групп в этом смысле нет:
    ``CREATE_NEW_PROCESS_GROUP`` — группа для консольных событий, её не
    опросить и не убить целиком, а дети мёртвого лидера не видны ни
    ``terminate``, ни ``kill``. Там снос по-прежнему ждёт одного лидера, и
    его дети могут пережить агента — ограничение платформы, которое снимает
    только Job Object.
    """
    pgid = _pgid_of(proc)
    if not _signal_tree(proc, pgid, kill=False):
        return
    try:
        if await _await_exit(proc, graceful_timeout, pgid):
            return
    except Exception:
        return
    _signal_tree(proc, pgid, kill=True)
    try:
        await _await_exit(proc, 2.0, pgid)
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
            dead = self._proc
            self._proc = None
            # И его читателя: реестр ключуется по id трубы, а запись мёртвого
            # процесса иначе оставалась там навсегда. Сервер, который падает
            # и перезапускается, копил бы их без предела.
            forget_stdio_reader(getattr(dead, "stdout", None))
            # И его группу: лидер мёртв, а дети его пережили — Chromium
            # упавшего @playwright/mcp остаётся сиротой, а новый сервер
            # поднимет второй. Снос тот же, что при закрытии (он же и пожнёт
            # лидера); дольше мгновения он идёт, только если сироты глухи к
            # SIGTERM. Loop'а здесь нет — `call` ходит через брошенный поток, —
            # поэтому свой, как у `close`; `run_coro_blocking` переживёт и
            # вызов с живого loop.
            try:
                run_coro_blocking(_aterminate_process_tree(dead, graceful_timeout=5.0))
            except Exception:
                pass

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

    def _detach(self) -> "Optional[subprocess.Popen]":
        """Снять процесс с сессии под локом. Дальше он уже ничей — ждать его
        можно сколько угодно, никого не блокируя.

        Лок держится ТОЛЬКО здесь. Раньше он удерживался на всё закрытие,
        включая ожидание close-тула (до 10 с) и смерти процесса (до 7 с); для
        асинхронного пути это недопустимо — ``threading.Lock``, удержанный
        через ``await``, застопорил бы весь loop, если бы за ним пришла
        корутина.
        """
        with self._lock:
            proc = self._proc
            self._proc = None
        return proc

    def _detach_nowait(self) -> "tuple[Optional[subprocess.Popen], bool]":
        """Снять процесс, НЕ вставая в очередь за локом.

        Возвращает ``(процесс, был_ли_лок_свободен)``. Второе важно: при
        занятом локе обмен по этому stdin недостоверен — см. вызывающих.

        Асинхронное закрытие нельзя пускать через блокирующий ``acquire``.
        Лок держит ``call()`` на весь обмен, и обмен идёт в
        abandonable-потоке, который переживает отмену вызывающего: сняли ход
        по дедлайну — поток остался в ``readline`` под локом, а ``arun`` в
        своём ``finally`` зовёт ``aclose_all`` и встаёт на ``with self._lock``
        прямо на loop. Замерено: закрытие 3.57 с, соседний хартбит успел один
        тик; в пределе это ``request_timeout_sec``, по умолчанию 600 с.

        Не смогли взять лок — забираем процесс без него. Гонка здесь
        безобиднее заморозки: держатель лока читает уже ничейный процесс и
        получит обычную ошибку ввода-вывода, а мы гасим его сами. Но пролог
        (обмен close-тулом) в этом случае ПРОПУСКАЕТСЯ: держатель сидит в
        ``_read_matching`` на том же ридере, и ответ close-тула ушёл бы в его
        очередь. Мы бы прождали таймаут впустую и всё равно перешли к
        терминации — только на десять секунд позже.
        """
        if self._lock.acquire(blocking=False):
            try:
                proc = self._proc
                self._proc = None
                return proc, True
            finally:
                self._lock.release()
        proc = self._proc
        self._proc = None
        return proc, False

    def close(self) -> None:
        """Синхронное закрытие. Зовётся в том числе из ``atexit``.

        Лок берётся БЕЗ ожидания. Раньше здесь стоял ``_detach()`` с
        блокирующим ``with self._lock`` — та самая заморозка, от которой
        асинхронный близнец уже ушёл, только теперь на выходе из процесса и
        без таймаута: abandonable-поток остаётся в ``readline`` под локом,
        и `atexit` ждал бы его до ``request_timeout_sec`` (по умолчанию 600 с).
        До появления atexit-хука процесс просто выходил.
        """
        proc, had_lock = self._detach_nowait()
        if proc is not None:
            if had_lock:
                self._close_prologue(proc)
            # Форма терминации одна и корутинная. Loop'а нет в `atexit`, но
            # встраивающий зовёт `close_all` из своего `finally` — под живым
            # loop. Голый `asyncio.run` там бросал, общий `except` выше это
            # глушил, и отцепленный процесс оставался жить без хозяина.
            run_coro_blocking(_aterminate_process_tree(proc, graceful_timeout=5.0))

    async def aclose(self) -> None:
        """То же закрытие, но ожидание не занимает поток.

        Пролог (ответ close-тула) и терминация — единственное, что здесь
        долгое, и оба переведены на опрос.

        Прерванное закрытие добивает группу СРАЗУ. Отмена, таймаут
        вызывающего, отмена фона на выходе ``asyncio.run`` — любое прерывание
        посреди ожидания раньше обрывало эскалацию, и SIGKILL не уходил
        никогда: сервер, глухой к SIGTERM (Chromium под @playwright/mcp),
        переживал агента. Подобрать его потом некому — процесс отцеплен от
        сессии первым делом, а сессия к этому моменту снята и с реестра, так
        что хук на выход её не видит. Поэтому на ЛЮБОМ ``BaseException`` —
        синхронный SIGKILL группе и прерывание дальше. Ждать смерти здесь
        нельзя: прерванную корутину уже никто не ждёт, а ``GeneratorExit``
        не позволил бы и ``await``. Пожнёт зомби ``Popen`` сам.

        Процесс берётся из отцепления, до первого ``await``: прервать нас
        между ними нечему, а ``self._proc`` после отцепления уже пуст.
        """
        proc, had_lock = self._detach_nowait()
        if proc is None:
            return
        try:
            if had_lock:
                await self._aclose_prologue(proc)
            await _aterminate_process_tree(proc, graceful_timeout=5.0)
        except BaseException:
            _signal_tree(proc, _pgid_of(proc), kill=True)
            # Хвост пролога мог не дойти: прервали на ответе close-тула. Без
            # него читатель мёртвого процесса остался бы в реестре навсегда.
            self._close_prologue_tail(proc)
            raise

    def _close_prologue(self, proc: "subprocess.Popen") -> None:
        close_tool = self._close_tool_name()
        if close_tool is not None:
            try:
                req_id = self._write_close_tool(proc, close_tool)
                self._read_matching_on(proc, req_id, 10.0)
            except Exception:
                pass
        self._close_prologue_tail(proc)

    async def _aclose_prologue(self, proc: "subprocess.Popen") -> None:
        close_tool = self._close_tool_name()
        if close_tool is not None:
            try:
                req_id = self._write_close_tool(proc, close_tool)
                await self._aread_matching_on(proc, req_id, 10.0)
            except Exception:
                pass
        self._close_prologue_tail(proc)

    def _close_tool_name(self) -> "Optional[str]":
        """Имя тула, которым сервер гасит своих внешних детей, если он есть.

        Некоторые MCP-серверы поднимают detached-потомков: @playwright/mcp на
        macOS может запустить системный Google Chrome (ppid=1, вне нашей
        process group), и killpg до него не дотянется. Сначала даём серверу
        шанс погасить их штатно (например ``browser_close_all``) — CDP-коннект
        закроется корректно, Chrome завершится сам. Не ответил за таймаут —
        идём дальше и дожимаем SIGTERM/SIGKILL.
        """
        close_tool = self._server_cfg.get("persistent_session_close_tool")
        if isinstance(close_tool, str) and close_tool.strip():
            return close_tool.strip()
        return None

    def _write_close_tool(self, proc: "subprocess.Popen", close_tool: str) -> int:
        req_id = self._next_id
        self._next_id += 1
        self._write_line_on(
            proc,
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": close_tool, "arguments": {}},
            },
        )
        return req_id

    @staticmethod
    def _close_stdin(proc: "subprocess.Popen") -> None:
        # Некоторые MCP-серверы по EOF корректно завершают свой event loop.
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass

    def _close_prologue_tail(self, proc: "subprocess.Popen") -> None:
        """Общий ХВОСТ обоих закрытий: закрыть stdin и отцепить читателя.

        Порядок здесь несущий, и он менялся не из вкуса. Пока отцепление
        стояло в НАЧАЛЕ, ответ close-тула прочитать было некому: первый
        ``_LineReader`` уже стоял в ``readline()`` на той же трубе и забирал
        строку в очередь, которую после ``forget`` никто не читает, а
        ``_read_matching_on`` заводил ВТОРОЙ читатель и честно ждал свои 10
        секунд. Каждое закрытие сервера с настроенным
        ``persistent_session_close_tool`` (документированный playwright с
        ``browser_close_all``) стоило ровно 10 с — и браузер при этом не
        закрывался, ответ не был увиден. Плюс два потока читали один
        ``TextIOWrapper``.

        Отцеплять всё равно надо: читатель живёт столько же, сколько процесс,
        иначе его запись останется в реестре навсегда.
        """
        self._close_stdin(proc)
        forget_stdio_reader(getattr(proc, "stdout", None))

    def _write_line(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None:
            raise RuntimeError("persistent stdio MCP process is not running")
        self._write_line_on(proc, payload)

    @staticmethod
    def _write_line_on(proc: "subprocess.Popen", payload: Dict[str, Any]) -> None:
        """Написать в УЖЕ ИЗВЕСТНЫЙ процесс.

        Закрытие снимает процесс с сессии первым делом (``_detach``), поэтому
        обращаться к ``self._proc`` дальше нельзя — там уже ``None``.
        """
        if proc.stdin is None:
            raise RuntimeError("persistent stdio MCP process is not running")
        line = json.dumps(payload) + "\n"
        proc.stdin.write(line)
        proc.stdin.flush()

    @staticmethod
    def _match_line(line: "str | None", req_id: Any) -> "tuple[bool, Dict[str, Any]]":
        """``(готово, ответ)`` для одной прочитанной строки.

        Общий разбор для синхронного и асинхронного чтения: две копии этого
        цикла разъехались бы на первом же изменении формата.
        """
        if line is None:
            return True, {}
        stripped = line.strip()
        if not stripped:
            return False, {}
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return False, {}
        if not isinstance(obj, dict):
            return False, {}
        # notifications не имеют id — просто пропускаем.
        if "id" not in obj:
            return False, {}
        if obj.get("id") == req_id:
            return True, obj
        return False, {}

    async def _aread_matching_on(
        self, proc: "subprocess.Popen", req_id: Any, timeout: float
    ) -> Dict[str, Any]:
        """Асинхронный двойник чтения ответа — ожидание очереди без потока."""
        if proc.stdout is None:
            raise RuntimeError("persistent stdio MCP process is not running")
        for _ in range(32):
            done, obj = self._match_line(
                await _areadline_with_timeout(proc.stdout, timeout), req_id
            )
            if done:
                return obj
        return {}

    def _read_matching_on(
        self, proc: "subprocess.Popen", req_id: Any, timeout: float
    ) -> Dict[str, Any]:
        """Чтение ответа из УЖЕ ИЗВЕСТНОГО процесса (после ``_detach``)."""
        if proc.stdout is None:
            raise RuntimeError("persistent stdio MCP process is not running")
        for _ in range(32):
            done, obj = self._match_line(
                _readline_with_timeout(proc.stdout, timeout), req_id
            )
            if done:
                return obj
        return {}

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
    """Реестр долгоживущих stdio-сессий, ключ — ``(владелец, server_key)``.

    Владелец (см. ``gc_core.mcp_session_owner``) в ключе обязателен. Без него ключом было
    одно лишь имя сервера, и ``close_all()`` из ``arun`` первого доигравшего
    агента убивал браузер, в котором в этот момент работали остальные. Симптом
    был тихим — ``_ensure_started`` молча поднимал процесс заново, — и виден
    только по времени: в прогоне трёх агентов один занял 44.8 с вместо 18.8 с
    у соседа на той же задаче, потому что дважды ждал старта Chromium.
    """

    _instance: Optional["PersistentStdioRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # (owner, server_key) -> сессия
        self._sessions: Dict[tuple, PersistentStdioSession] = {}
        self._lock = threading.Lock()
        self._exit_hook_armed = False

    def _arm_exit_hook(self) -> None:
        """Один раз повесить закрытие безвладельческих сессий на выход процесса."""
        if self._exit_hook_armed:
            return
        with self._lock:
            if self._exit_hook_armed:
                return
            self._exit_hook_armed = True
        import atexit

        atexit.register(self.close_everything)

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
        owner = current_owner()
        key = (owner, server_key)
        # Хук на выход ставим ВСЕГДА. Раньше — только для безвладельческих
        # сессий: их не закроет ни один `arun`, потому что ни один прогон их не
        # заводил (прямой `apply_tool`, `gc-mcp`), а обещанного докстрокой
        # `mcp_session_owner` явного `close_everything()` не было НИ В ОДНОМ
        # пакете. Но владельческой сессии подстраховка нужна ровно так же: её
        # закрывает `aclose_all` из `finally` прогона, и если этот `finally`
        # сняли, остаток не увидит никто — снос фильтрует по владельцу, а
        # следующий прогон приходит с новым. Хук стоит один `atexit.register`
        # на процесс; при SIGKILL не отработает, но это разница между «обычно
        # закрываем» и «не закрываем никогда».
        self._arm_exit_hook()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = PersistentStdioSession(server_cfg)
                self._sessions[key] = session
            return session

    def _take_own(self) -> list:
        """Снять с реестра сессии ТЕКУЩЕГО владельца. Чужие не трогаем."""
        owner = current_owner()
        with self._lock:
            keys = [k for k in self._sessions if k[0] == owner]
            return [self._sessions.pop(k) for k in keys]

    def _take_one_own(self):
        """Снять ОДНУ сессию текущего владельца. ``None`` — своих больше нет.

        По одной, а не пачкой, ради устойчивости к отмене: ``aclose_all``
        вынимал все сразу и закрывал последовательно, и ``CancelledError``
        посреди списка оставлял остаток ВНЕ реестра — то есть незакрытым и
        уже никому не видимым. Для playwright это значит, что Chromium
        переживает агента.
        """
        owner = current_owner()
        with self._lock:
            for key in list(self._sessions):
                if key[0] == owner:
                    return self._sessions.pop(key)
        return None

    async def _adrain_own_within_budget(self, budget: float) -> None:
        """Дочистить свои сессии с ОБЩИМ бюджетом. Зовут под уже снятой задачей.

        На что бюджета не хватило — НЕ снимаем с реестра: там остаток застанет
        ``close_everything`` на выходе процесса. Поэтому бюджет проверяется ДО
        снятия сессии, а не после.

        Сессию, которую сняли, но закрыть в остаток бюджета не успели, таймаут
        ОТМЕНЯЕТ, и ``aclose`` на прерывании сразу добивает её группу. Раньше
        закрытие шло под ``shield`` и по таймауту доигрывало в фоне, но фон
        на выходе ``asyncio.run`` снимается отменой, и эскалация обрывалась
        там же: после выхода агента глухие к SIGTERM серверы оставались жить,
        а из реестра они уже сняты. Вернуть такую сессию в реестр нельзя —
        ``aclose`` первым делом отцепляет процесс, и ``close_everything``
        нашёл бы в ней пустое место.
        """
        deadline = time.monotonic() + budget
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            session = self._take_one_own()
            if session is None:
                return
            try:
                # Без `shield`: по таймауту закрытие отменяется, и группа
                # добивается SIGKILL'ом сразу, а не бросается доигрывать в фон.
                await asyncio.wait_for(session.aclose(), left)
            except asyncio.CancelledError:
                # Сняли ПОВТОРНО. Настаивать не на чем: исходную отмену
                # вызывающий получит в любом случае. Закрывавшуюся сессию
                # `aclose` уже добил, оставшиеся в реестре доберёт выход.
                return
            except Exception:
                pass

    def close_all(self) -> None:
        """Закрыть сессии текущего владельца. Синхронный вход."""
        for session in self._take_own():
            try:
                session.close()
            except Exception:
                pass

    async def aclose_all(self) -> None:
        """То же, не занимая поток из пула.

        Цикл агента гасит сессии в своём ``finally``, и это до 7 с на сессию
        (SIGTERM → таймаут → SIGKILL) плюс до 10 с на ответ close-тула. Раньше
        всё это уезжало в ``asyncio.to_thread``; теперь ожидание — опрос, и
        поток не нужен. Форма та же, что у ``stream_client``: синхронный
        ``close_all`` рядом с ``aclose_all``.

        Отмена приходит из двух РАЗНЫХ мест, и поступать с ними надо
        по-разному; различаем по счётчику ``Task.cancelling()``:

        * **сняли НАС** (счётчик вырос) — дочищаем остаток с общим бюджетом
          ``_CANCEL_DRAIN_BUDGET_SEC`` и пробрасываем отмену дальше. Глотать
          её нельзя: вызывающий обязан узнать, что его сняли. Работать же без
          ограничения нельзя тем более — это держит снятый прогон ещё по 7 с
          на сессию.
        * **отменилась САМА сессия** — ``CancelledError`` прилетел из её
          ``aclose`` (обычно из anyio cancel scope). Тогда копим и закрываем
          остальные: прежний ``raise`` прямо из цикла гасил ровно одну, а
          прочие оставались в реестре и ЖИВЫМИ.

        Почему остаток нельзя просто оставить в реестре «до следующего
        сноса», как здесь было написано раньше: снос фильтрует по владельцу, а
        следующий владелец другой. У сервера контекст новый на каждый вебхук,
        у CLI прогон один; переиспользует владельца только TUI. Последняя
        сетка — ``atexit``, и ради неё же хук теперь ставится всегда, а не
        только на безвладельческом пути. Для playwright цена прежняя: Chromium
        переживает агента.
        """
        task = asyncio.current_task()
        outer_cancels = task.cancelling() if task is not None else 0
        cancelled = None
        while True:
            session = self._take_one_own()
            if session is None:
                break
            try:
                # `shield`: закрытие сессии обязано ДОЙТИ до конца, даже если
                # вызывающего сняли прямо сейчас. Это `finally` цикла агента,
                # и оборванный на середине teardown оставляет чужой процесс
                # (браузер) жить дальше. Доигрывает оно в фоне, и это
                # безопасно: снимут фон (выход `asyncio.run` отменяет всё
                # оставшееся) — `aclose` на прерывании добьёт группу SIGKILL'ом.
                # Убирать `shield` отсюда, как из дочистки, нельзя: без него
                # отмена сразу отдавала бы этой сессии SIGKILL, не дав
                # close-тулу погасить потомков сервера вне нашей группы
                # (системный Chrome) — до них killpg не дотянется.
                await asyncio.shield(session.aclose())
            except asyncio.CancelledError as exc:
                if task is not None and task.cancelling() > outer_cancels:
                    # Сняли нас: дочищаем остаток в пределах общего бюджета и
                    # отдаём отмену наружу. Что не успели — ждёт выхода
                    # процесса, хук на который теперь стоит всегда.
                    await self._adrain_own_within_budget(_CANCEL_DRAIN_BUDGET_SEC)
                    raise
                cancelled = exc            # отменилась сессия: гасим остальные
            except Exception:
                pass
        if cancelled is not None:
            raise cancelled

    def close_everything(self) -> None:
        """Закрыть ВСЕ сессии, включая чужие. Завершение процесса и тесты.

        Нужен отдельным именем: обычный ``close_all`` теперь намеренно щадит
        чужих, а на выходе из процесса щадить некого — иначе Chromium
        переживёт агента, ради чего всё и затевалось.
        """
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
