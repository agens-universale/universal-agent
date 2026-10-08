"""Проба живости удалённой оболочки: ``echo $((a+b))``.

В живом окружении сумма всегда возвращается отдельной строкой и с кодом 0.
Не вернулась — среда не ответила. Текст ошибки не разбираем: проба как раз
заменяет такой разбор. Каталог в команду не входит, чтобы стёртый cwd не
выглядел как мёртвый контейнер.

Вызов пробы идёт в обход самой пробы, иначе ``echo`` проверял бы сам себя.
"""

from __future__ import annotations

import random
import re
from typing import Any, Awaitable, Callable

# Потолок пробы, не таймаут команды. Мёртвый демон отвечает сразу, живая
# сумма тоже; 60 с оставляют запас медленному, но живому docker exec.
# Слагаемые двузначные и без ведущего нуля: `08` в `$((…))` — восьмеричное.
_RUNTIME_PROBE_TIMEOUT_SEC = 60
_RUNTIME_PROBE_NUDGE = (
    "Runtime connection failed. Wait a few seconds and retry this command. "
    "If several retries with a wait still fail, report it with turn_finish: "
    "the environment is down and will not recover in this run."
)
_PROBE_COMMAND_RE = re.compile(r"echo \$\(\(([1-9][0-9])\+([1-9][0-9])\)\)")

# ``run(command, timeout) -> result`` — сырой exec без повторной пробы.
ProbeRun = Callable[[str, int], Awaitable[Any]]


class RuntimeUnavailable(Exception):
    """Оболочка не вернула сумму. ``str(self)`` — совет модели."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        return_code: int | None = None,
        timed_out: bool = False,
    ) -> None:
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        self.return_code = return_code
        self.timed_out = timed_out
        super().__init__(_probe_failure_message(self.stdout, self.stderr))


def _probe_terms() -> tuple[int, int]:
    return random.randint(10, 99), random.randint(10, 99)


def _probe_command(left: int, right: int) -> str:
    return f"echo $(({left}+{right}))"


def probe_total_in_command(command: str) -> int | None:
    """Сумма из текста пробы, если команда её содержит. Для подставных бэкендов."""
    match = _PROBE_COMMAND_RE.search(command or "")
    if match is None:
        return None
    return int(match.group(1)) + int(match.group(2))


def probe_stdout(command: str) -> str | None:
    """Ответ живой пробы: строка с суммой. ``None`` — это не проба.

    Фейки окружения возвращают её до своей логики, не записывая вызов:
    иначе каждая проверка ``env.calls`` видела бы лишний ``echo``.
    """
    total = probe_total_in_command(command)
    if total is None:
        return None
    return f"{total}\n"


def _probe_came_back(res: Any, total: int) -> bool:
    """Среди строк stdout есть ровно сумма, код возврата 0.

    Лишние строки профиля не мешают. Эхо текста команды сумму не содержит,
    поэтому цитата «command … не выполнена» пробу не проходит.
    """
    if getattr(res, "timed_out", False) or res.return_code != 0:
        return False
    expect = str(total)
    stdout = getattr(res, "stdout", None) or ""
    return any(line.strip() == expect for line in stdout.splitlines())


def _probe_failure_message(stdout: str, stderr: str) -> str:
    detail = "\n".join(
        part.strip() for part in (stdout, stderr) if part and part.strip()
    )
    if detail:
        return f"{_RUNTIME_PROBE_NUDGE} ({detail})"
    return _RUNTIME_PROBE_NUDGE


def _probe_timeout(timeout: int | None) -> int:
    """Потолок пробы — 60 с, но не дольше таймаута самого действия.

    Мёртвый демон отвечает сразу. Зависший транспорт должен упереться в
    таймаут операции, а не ждать отдельные 60 с до неё.
    """
    if timeout is None or timeout <= 0:
        return _RUNTIME_PROBE_TIMEOUT_SEC
    return min(int(timeout), _RUNTIME_PROBE_TIMEOUT_SEC)


async def aensure_runtime(
    run: ProbeRun, *, timeout: int | None = None
) -> None:
    """Один раз убедиться, что ``run`` исполняет оболочку. Иначе ``RuntimeUnavailable``.

    ``run`` обязан не звать пробу снова.
    """
    left, right = _probe_terms()
    try:
        res = await run(_probe_command(left, right), _probe_timeout(timeout))
    except RuntimeUnavailable:
        raise
    except Exception as exc:
        err = str(exc).replace("\n", " ").strip()
        raise RuntimeUnavailable(stderr=err) from exc
    if _probe_came_back(res, left + right):
        return
    raise RuntimeUnavailable(
        stdout=getattr(res, "stdout", None) or "",
        stderr=getattr(res, "stderr", None) or "",
        return_code=getattr(res, "return_code", None),
        timed_out=bool(getattr(res, "timed_out", False)),
    )
