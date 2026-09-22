"""Снос процессов долгоживущего stdio MCP-сервера: после него в группе никого.

Два свойства одного обещания.

* **Снос ждёт группу, а не лидера.** Сервер запускается лидером собственной
  группы (``start_new_session``), и тяжёлое у него обычно в ДЕТЯХ:
  @playwright/mcp — это node, а Chromium под ним живёт в той же группе и
  SIGTERM игнорирует. Снос смотрел только на лидера: его смерть засчитывалась
  за смерть всех, уже мёртвого лидера снос и вовсе пропускал, а подмена
  упавшего сервера новым не трогала детей старого.
* **Прерванное закрытие добивает группу сразу.** Закрытие глухого к SIGTERM
  сервера — это ожидание graceful-таймаута перед SIGKILL. Отмена или таймаут
  посреди ожидания обрывали эскалацию, и SIGKILL не уходил никогда, а процесс
  к этому моменту уже отцеплен от сессии и сессия — от реестра: хук на выход
  его не видит.

Двойник сервера здесь НАСТОЯЩИЙ процесс, запущенный продовым
``_spawn_detached_stdio_process``: свойство звучит как «после сноса в группе
никого», и проверяется оно только на живых процессах. По заказу двойник
поднимает потомка в своей группе и по-разному отвечает на SIGTERM — выходит
или глохнет. Каждый полученный SIGTERM он отмечает в файле, чтобы тест мог
дождаться «закрытие уже ждёт после SIGTERM», а не угадывать по времени.

Всё, что тест запустил, добивает фикстура ``reaper`` в любом исходе.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp import persistent_stdio as ps
from gc_testkit.paths import package_src_dirs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="группы процессов и killpg есть только на POSIX")

#: Двойник MCP-сервера. Отвечает на любой запрос пустым результатом, на
#: ``tools/call`` с именем ``crash`` падает, не ответив. Когда stdin закрыт,
#: не выходит: уйти он может только по сигналу — как Chromium, которому до
#: stdin сервера нет дела.
_SERVER = r'''
import json, os, signal, subprocess, sys, time

MARK = os.environ["GC_TEST_MARK"]
PIDS = os.environ["GC_TEST_PIDS"]
LEADER_ON_TERM = os.environ.get("GC_TEST_LEADER_ON_TERM", "ignore")
CHILD_ON_TERM = os.environ.get("GC_TEST_CHILD_ON_TERM", "")

CHILD = """
import os, signal, time
def on_term(signum, frame):
    with open(os.environ["GC_TEST_MARK"], "a") as f:
        f.write("child TERM\\n")
    if os.environ.get("GC_TEST_CHILD_ON_TERM") == "exit":
        os._exit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
while True:
    time.sleep(1)
"""

def on_term(signum, frame):
    with open(MARK, "a") as f:
        f.write("leader TERM\n")
    if LEADER_ON_TERM == "exit":
        os._exit(0)

signal.signal(signal.SIGTERM, on_term)

child_pid = None
if CHILD_ON_TERM:
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    child.stdout.readline()
    child_pid = child.pid

with open(PIDS, "a") as f:
    f.write(json.dumps({"leader": os.getpid(), "child": child_pid}) + "\n")

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    rid = req.get("id")
    if rid is None:
        continue
    if (req.get("params") or {}).get("name") == "crash":
        os._exit(3)
    result = {"tools": []} if req.get("method") == "tools/list" else {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()

while True:
    time.sleep(1)
'''


def _cfg(tmp_path, *, leader_on_term: str = "ignore", child_on_term: str = "") -> dict:
    """Конфиг сервера-двойника. ``child_on_term`` пустой — потомка нет."""
    script = tmp_path / "stubborn_mcp_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "persistent_session": True,
        "env": {
            "GC_TEST_MARK": str(tmp_path / "signals.log"),
            "GC_TEST_PIDS": str(tmp_path / "pids.jsonl"),
            "GC_TEST_LEADER_ON_TERM": leader_on_term,
            "GC_TEST_CHILD_ON_TERM": child_on_term,
        },
    }


def _rows(tmp_path) -> list:
    """Отчёты двойников о запуске. Недописанную последнюю строку пропускаем."""
    path = tmp_path / "pids.jsonl"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.split("\n")[:-1] if line.strip()]


def _wait_rows(tmp_path, count: int, timeout: float = 15.0) -> list:
    deadline = time.monotonic() + timeout
    while True:
        rows = _rows(tmp_path)
        if len(rows) >= count:
            return rows
        if time.monotonic() >= deadline:
            raise AssertionError(f"двойник сервера не отчитался о запуске: {rows}")
        time.sleep(0.02)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_gone_within(pgid: int, timeout: float) -> bool:
    """Опустела ли группа за ``timeout``. Внуков пожинает init — ему нужен миг."""
    deadline = time.monotonic() + timeout
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


@pytest.fixture
def reaper(tmp_path):
    """Добить всё запущенное тестом в любом исходе: сирот не оставляем.

    Группы берутся и из прямых детей теста, и из отчётов самих двойников: так
    добиваются потомки и процессы, поднятые не нами. Прямых детей ещё и
    пожинаем, иначе они повиснут зомби.
    """
    procs: list = []
    yield procs
    groups = [row["leader"] for row in _rows(tmp_path)] + [p.pid for p in procs]
    for pgid in groups:
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


def _spawn(cfg: dict, reaper: list) -> subprocess.Popen:
    """Запустить двойника продовым путём — лидером собственной группы."""
    proc = ps._spawn_detached_stdio_process(cfg)
    reaper.append(proc)
    return proc


# --- снос ждёт группу целиком ------------------------------------------------


async def test_a_child_deaf_to_sigterm_does_not_outlive_its_leader(tmp_path, reaper):
    """Лидер ушёл по SIGTERM, потомок в его группе SIGTERM проигнорировал.

    Ровно @playwright/mcp: node выходит сразу, Chromium глух. Ожидание
    смотрело только на лидера, его смерть засчитывалась за смерть всех, и
    до SIGKILL группе дело не доходило никогда.
    """
    leader = _spawn(_cfg(tmp_path, leader_on_term="exit", child_on_term="ignore"), reaper)
    child = _wait_rows(tmp_path, 1)[0]["child"]

    await ps._aterminate_process_tree(leader, graceful_timeout=0.3)

    assert leader.poll() is not None, "лидер пережил снос"
    assert _group_gone_within(leader.pid, 2.0), (
        f"потомок {child} пережил снос группы: ждали только лидера, и SIGKILL группе так и не ушёл"
    )


async def test_a_dead_leader_does_not_shield_its_group(tmp_path, reaper):
    """Лидер уже мёртв, его потомок жив — снос обязан добить потомка.

    Раньше мёртвый лидер означал ранний выход, группу не трогали вовсе. Так
    выглядит упавший сервер: его Chromium остаётся сиротой.
    """
    leader = _spawn(_cfg(tmp_path, child_on_term="ignore"), reaper)
    child = _wait_rows(tmp_path, 1)[0]["child"]
    os.kill(leader.pid, signal.SIGKILL)  # только лидера, не группу
    leader.wait(timeout=5)
    assert _group_alive(leader.pid), "проверка пустая: потомок умер вместе с лидером"

    await ps._aterminate_process_tree(leader, graceful_timeout=0.3)

    assert _group_gone_within(leader.pid, 2.0), f"потомок {child} мёртвого лидера пережил снос: группу не тронули"


async def test_an_obedient_group_is_not_kept_waiting(tmp_path, reaper):
    """Обратная сторона: группа, ушедшая по SIGTERM, не ждёт graceful-таймаута.

    Ожидание группы легко сделать вечно недовольным — например, опрашивать её,
    не пожав лидера: зомби числится в группе, и ``killpg(pgid, 0)`` не ответит
    «никого» никогда. Тогда каждый снос стоил бы полный таймаут.
    """
    leader = _spawn(_cfg(tmp_path, leader_on_term="exit", child_on_term="exit"), reaper)
    _wait_rows(tmp_path, 1)

    started = time.monotonic()
    await ps._aterminate_process_tree(leader, graceful_timeout=5.0)
    spent = time.monotonic() - started

    assert _group_gone_within(leader.pid, 1.0), "послушная группа пережила снос"
    assert spent < 2.0, f"снос послушной группы занял {spent:.2f} с — ждали, когда ждать нечего"


def test_a_crashed_server_is_replaced_without_leaving_its_group(tmp_path, reaper):
    """Упавший сервер подменяется новым — дети старого жить не остаются.

    ``_ensure_started`` видел мёртвого лидера, пожинал его и поднимал новый
    процесс; потомков старого не трогал никто. Каждое падение сервера
    оставляло по браузеру.
    """
    session = ps.PersistentStdioSession(_cfg(tmp_path, leader_on_term="exit", child_on_term="exit"))
    try:
        session.call("tools/list", {}, 10.0)
        first = session._proc
        reaper.append(first)
        child = _wait_rows(tmp_path, 1)[0]["child"]

        session.call("tools/call", {"name": "crash", "arguments": {}}, 5.0)
        first.wait(timeout=5)  # сервер упал между вызовами
        assert _group_alive(first.pid), "проверка пустая: потомок упал вместе с сервером"

        session.call("tools/list", {}, 10.0)  # подмена мёртвого лидера
        reaper.append(session._proc)

        assert session._proc is not first, "сервер не перезапущен — проверять нечего"
        assert _group_gone_within(first.pid, 2.0), f"потомок {child} упавшего сервера пережил его перезапуск"
    finally:
        session.close()


# --- прерванное закрытие добивает группу сразу -------------------------------


def _started_session(tmp_path, reaper: list, **cfg_kw) -> ps.PersistentStdioSession:
    """Сессия с поднятым сервером — продовым путём, через первый вызов."""
    session = ps.PersistentStdioSession(_cfg(tmp_path, **cfg_kw))
    session.call("tools/list", {}, 10.0)
    reaper.append(session._proc)
    return session


async def _until_leader_signalled(tmp_path, timeout: float = 10.0) -> None:
    """Дождаться, что SIGTERM дошёл до лидера: закрытие уже ждёт после него."""
    mark = tmp_path / "signals.log"
    deadline = time.monotonic() + timeout
    while "leader TERM" not in (mark.read_text(encoding="utf-8") if mark.exists() else ""):
        if time.monotonic() >= deadline:
            raise AssertionError("SIGTERM до лидера так и не дошёл")
        await asyncio.sleep(0.02)


def _dead_within(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


#: Сколько даём на «добит сразу». Граница по другую сторону — graceful-таймаут
#: закрытия, 5 с: столько процесс прожил бы, дойди эскалация до SIGKILL сама.
_AT_ONCE_SEC = 1.5


async def test_a_cancelled_close_kills_the_group_at_once(tmp_path, reaper):
    """Отмена посреди закрытия — SIGKILL группе сразу, а не никогда."""
    session = _started_session(tmp_path, reaper, child_on_term="ignore")
    proc = session._proc

    closing = asyncio.create_task(session.aclose())
    await _until_leader_signalled(tmp_path)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert _dead_within(proc, _AT_ONCE_SEC), (
        "лидер жив после прерванного закрытия: эскалация оборвалась, SIGKILL не ушёл"
    )
    assert _group_gone_within(proc.pid, _AT_ONCE_SEC), "потомок жив после прерванного закрытия"


async def test_a_timed_out_close_kills_the_group_at_once(tmp_path, reaper):
    """То же для таймаута вызывающего: ``wait_for`` вокруг закрытия — обычное дело."""
    session = _started_session(tmp_path, reaper, child_on_term="ignore")
    proc = session._proc

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(session.aclose(), 0.5)

    assert _dead_within(proc, _AT_ONCE_SEC), "лидер жив после закрытия, снятого по таймауту"
    assert _group_gone_within(proc.pid, _AT_ONCE_SEC), "потомок жив после закрытия, снятого по таймауту"


async def test_the_drain_does_not_abandon_a_session_it_took(tmp_path, reaper):
    """Дочистка сняла сессию с реестра и не уложилась в бюджет — процесс добит.

    Бюджет дочистки общий, 3 с, а закрытие глухого к SIGTERM сервера — 5 с до
    SIGKILL. ``wait_for`` по таймауту оставлял такое закрытие доигрывать в
    фоне под ``shield``, а фон на выходе ``asyncio.run`` снимается отменой —
    и эскалация обрывалась там же. Сессия к этому моменту снята с реестра, и
    хук на выход её не видит.
    """
    registry = ps.PersistentStdioRegistry()
    session = _started_session(tmp_path, reaper, child_on_term="ignore")
    proc = session._proc
    registry._sessions[("drain-owner", "srv")] = session

    with use_owner("drain-owner"):
        await registry._adrain_own_within_budget(0.5)

    assert registry._sessions == {}, "проверка пустая: дочистка сессию не взяла"
    assert _dead_within(proc, _AT_ONCE_SEC), (
        "сессия снята с реестра, бюджет вышел, а её процесс жив — его больше не увидит никто, включая хук на выход"
    )
    assert _group_gone_within(proc.pid, _AT_ONCE_SEC), "потомок сессии пережил дочистку"


#: «Агент» для сценария ревью целиком: две глухие к SIGTERM сессии, снос
#: снимают снаружи посреди закрытия первой, а бюджет дочистки меньше, чем
#: закрытие второй. После этого агент просто выходит.
_AGENT = r"""
import asyncio, json, sys
from pathlib import Path

from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp import persistent_stdio as ps

ps._CANCEL_DRAIN_BUDGET_SEC = 0.5
cfg = json.loads(sys.argv[1])
mark = Path(cfg["env"]["GC_TEST_MARK"])


async def main():
    registry = ps.PersistentStdioRegistry.instance()
    with use_owner("agent"):
        for name in ("first", "second"):
            registry.get_or_create(name, cfg).call("tools/list", {}, 10.0)
        teardown = asyncio.create_task(registry.aclose_all())
        while "leader TERM" not in (mark.read_text() if mark.exists() else ""):
            await asyncio.sleep(0.02)
        teardown.cancel()
        try:
            await teardown
        except asyncio.CancelledError:
            pass


asyncio.run(main())
"""


def test_no_server_survives_the_agent_after_an_interrupted_teardown(tmp_path, reaper):
    """Сценарий ревью целиком, в настоящем процессе агента.

    Первая сессия закрывается под ``shield`` и доигрывает в фоне, вторая
    попадает в дочистку и в бюджет не укладывается. Агент выходит, и
    ``asyncio.run`` снимает оставшиеся закрытия отменой. Раньше после этого
    жили ВСЕ глухие к SIGTERM серверы (ревью: «живы 2 из 3»): эскалация
    обрывалась, из реестра они уже сняты, и хук на выход их не видел.
    """
    env = dict(os.environ)
    # Исходники рабочей копии: без них агент взял бы установленную копию
    # gc_core и проверял бы не тот код.
    src_dirs = [str(d) for d in package_src_dirs()]
    env["PYTHONPATH"] = os.pathsep.join(src_dirs + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    cfg = _cfg(tmp_path, child_on_term="ignore")

    done = subprocess.run(
        [sys.executable, "-c", _AGENT, json.dumps(cfg)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    rows = _rows(tmp_path)
    assert len(rows) == 2, f"проверка пустая: агент поднял не два сервера: {rows}"
    survivors = [row for row in rows if not _group_gone_within(row["leader"], 2.0)]
    assert survivors == [], f"после выхода агента живы серверы с потомками: {survivors}"


# --- синхронное закрытие из-под живого loop ----------------------------------


async def test_a_sync_close_under_a_running_loop_still_kills_the_group(tmp_path, reaper):
    """``close_everything`` из ``finally`` встраивающего — это живой loop.

    Докстрока реестра так и советует: гасить сессии в своём ``finally``, а там
    обычно работает loop. Синхронный ``close`` заводил собственный
    ``asyncio.run``, тот бросал «cannot be called from a running event loop»,
    общий ``except`` ошибку глушил — сессия снята с реестра, а её процесс жив,
    и подобрать его больше некому.
    """
    registry = ps.PersistentStdioRegistry()
    session = _started_session(tmp_path, reaper, leader_on_term="exit", child_on_term="exit")
    proc = session._proc
    registry._sessions[("embedder", "srv")] = session

    registry.close_everything()  # синхронно, прямо на loop теста

    assert registry._sessions == {}, "проверка пустая: закрытие сессию не взяло"
    assert _dead_within(proc, _AT_ONCE_SEC), "сессия снята с реестра, а её процесс жив"
    assert _group_gone_within(proc.pid, _AT_ONCE_SEC), "потомок сессии пережил закрытие"
