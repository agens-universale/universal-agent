"""Проба живости удалённой оболочки: ``echo $((a+b))``.

В живом окружении сумма всегда возвращается отдельной строкой и с кодом 0.
Не вернулась — среда не ответила. Текст ошибки не разбираем: проба как раз
заменяет такой разбор. Каталог в команду не входит, чтобы стёртый cwd не
выглядел как мёртвый контейнер.

Вызов пробы идёт в обход самой пробы, иначе ``echo`` проверял бы сам себя.
Успешная проба помнится ``RuntimeProbeCache`` несколько секунд: пока отметка
свежая, следующий echo не шлётся. Отказ не запоминается.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

# Успешный echo закрывает следующие действия на этот срок. 0 — проба каждый раз.
# Дольше 10 с мёртвый контейнер слишком долго выглядит живым.
DEFAULT_RUNTIME_PROBE_TTL_SEC = 10

# Потолок пробы, не таймаут команды. Мёртвый демон отвечает сразу, живая
# сумма тоже; 60 с оставляют запас медленному, но живому docker exec.
# Слагаемые двузначные и без ведущего нуля: `08` в `$((…))` — восьмеричное.
_RUNTIME_PROBE_TIMEOUT_SEC = 60
_RUNTIME_PROBE_NUDGE = (
    "Runtime connection failed. Wait a few seconds and retry this command. "
    "If several retries with a wait still fail, report it with turn_finish: "
    "the environment is down and will not recover in this run."
)
# Сколько символов ответа пробы класть в надж и в потоки исключения.
# Сумма — одна строка; 500 хватает на баннер и текст ошибки.
_PROBE_FAILURE_DETAIL_CHARS = 500

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
        self.stdout = _clip_probe_text(stdout or "")
        self.stderr = _clip_probe_text(stderr or "")
        self.return_code = return_code
        self.timed_out = timed_out
        super().__init__(_probe_failure_message(self.stdout, self.stderr))


def reraise_runtime_unavailable(exc: BaseException) -> None:
    """Отказ среды не прятать в соседней ветке ``except Exception``."""
    if isinstance(exc, RuntimeUnavailable):
        raise exc


def _probe_terms() -> tuple[int, int]:
    return random.randint(10, 99), random.randint(10, 99)


def _probe_command(left: int, right: int) -> str:
    return f"echo $(({left}+{right}))"


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


def _clip_probe_text(text: str) -> str:
    """Начало ответа. Дальше модели не нужно: суммы там уже нет."""
    if len(text) <= _PROBE_FAILURE_DETAIL_CHARS:
        return text
    return text[:_PROBE_FAILURE_DETAIL_CHARS] + "..."


def _probe_failure_message(stdout: str, stderr: str) -> str:
    detail = "\n".join(
        part.strip() for part in (stdout, stderr) if part and part.strip()
    )
    detail = _clip_probe_text(detail)
    if detail:
        return f"{_RUNTIME_PROBE_NUDGE} ({detail})"
    return _RUNTIME_PROBE_NUDGE


async def aensure_runtime(run: ProbeRun) -> None:
    """Убедиться, что ``run`` исполняет оболочку. Иначе ``RuntimeUnavailable``.

    Кэша здесь нет: его держит ``RuntimeProbeCache``. ``run`` обязан не звать
    пробу снова. Потолок echo — свои 60 с, не таймаут команды: живой ответ
    приходит сразу, а ``timeout: 1`` не объявляет медленный docker мёртвым.
    """
    left, right = _probe_terms()
    try:
        res = await run(
            _probe_command(left, right), _RUNTIME_PROBE_TIMEOUT_SEC
        )
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


class RuntimeProbeCache:
    """Успешная проба жива ``ttl_sec`` секунд. Отказ проверяется снова сразу.

    Параллельные вызовы делят один echo: второй ждёт первый и, если тот
    прошёл, свой не шлёт.
    """

    def __init__(self, ttl_sec: int = DEFAULT_RUNTIME_PROBE_TTL_SEC) -> None:
        self.ttl_sec = ttl_sec
        self._ok_until = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self.ttl_sec > 0 and time.monotonic() < self._ok_until

    async def ensure(self, run: ProbeRun) -> None:
        if self._fresh():
            return
        if self.ttl_sec <= 0:
            await aensure_runtime(run)
            return
        async with self._lock:
            if self._fresh():
                return
            await aensure_runtime(run)
            self._ok_until = time.monotonic() + self.ttl_sec
