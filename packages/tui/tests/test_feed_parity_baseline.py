"""Страж «открытие сессии = живой путь» на фикстурах истории (спека шага 2
окна ленты, §8 «Паритет»).

Одни и те же записи треда, открытые как сессия и поданные живым путём
(пачками, опросом хвоста), дают одну последовательность строк ленты за
вычетом эфемерного. Сравнение — после записи итога последнего хода."""

from __future__ import annotations

import pytest
from feed_dump import (
    HISTORY_THREAD,
    feed_dump,
    feed_live,
    load_history_fixture,
    without_ephemeral,
    write_history,
)
from gc_testkit.tui_support import pinned_snapshot_clock, tui_settings

HISTORY_FIXTURES = [
    "calls",
    "compaction",
    "subagents",
    "ask_human",
    "withdrawn",
    "model_change",
    "plan",
    "finish_attempts",
]

#: Расхождения нынешнего кода «живой путь ≠ открытие»: фикстура → причина.
#: Тест такой фикстуры — строгий xfail: совпадение роняет его, и запись
#: снимается. Список может только сокращаться.
KNOWN_LIVE_VS_OPEN: dict[str, str] = {}

_SIZE = (160, 45)
_SETTLE_STEPS = 40
#: Реестр моделей фикстуры смены модели (стенд писал её с этим реестром).
_SETTINGS = {
    "agent.model": "M",
    "models": {
        "fast-model": {"url": "http://127.0.0.1:9/v1", "token": ""},
        "strong-model": {"url": "http://127.0.0.1:9/v1", "token": ""},
    },
    "slots": {"default": "fast-model", "max": "strong-model"},
}


def _app(threads_dir):
    # Импорт приложения — в тесте: палитра выбирается до импорта gc_tui.app.
    from gc_tui.app import TuiApp

    threads_dir.mkdir()
    return TuiApp(
        threads_dir=str(threads_dir),
        system="cli",
        settings=tui_settings(_SETTINGS),
    )


async def _settled_dump(pilot) -> list:
    """Дамп ленты, когда он перестал меняться (отложенная отрисовка
    блоков под ширину ленты)."""
    feed = pilot.app.screen.query_one("#trace")
    dump = feed_dump(feed)
    for _ in range(_SETTLE_STEPS):
        await pilot.pause(0.05)
        again = feed_dump(feed)
        if again == dump:
            return dump
        dump = again
    raise AssertionError("лента не успокоилась")


async def dump_after_open(name: str, tmp_path):
    """Тред фикстуры записан целиком — экран открывает сессию."""
    fixture = load_history_fixture(name)
    async with _app(tmp_path / "open").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        write_history(str(tmp_path / "open"), fixture)
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        return await _settled_dump(pilot)


async def dump_after_live_feed(name: str, tmp_path, *, splits):
    """Экран открыт на пустом треде — записи дописываются пачками."""
    fixture = load_history_fixture(name)
    async with _app(tmp_path / "live").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        feed_live(scr, fixture.records, splits=splits, sidecar=fixture.sidecar)
        return await _settled_dump(pilot)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", HISTORY_FIXTURES)
#: (2, 9) — ответ модели отрезан от своего вызова: в calls, ask_human,
#: finish_attempts и compaction запись 9 — turn_finish за текстом ответа
#: (запись 8), запись 2 — вызов за текстом шага (запись 1).
@pytest.mark.parametrize("splits", [(), (3,), (1, 4, 7), (2, 9)])
async def test_open_matches_live_feed(
    name, splits, tmp_path, request, monkeypatch
):
    if name in KNOWN_LIVE_VS_OPEN:
        request.applymarker(
            pytest.mark.xfail(reason=KNOWN_LIVE_VS_OPEN[name], strict=True)
        )
    # Время строк итога — от одних часов в обоих путях.
    with pinned_snapshot_clock(monkeypatch):
        opened = await dump_after_open(name, tmp_path)
        live = await dump_after_live_feed(name, tmp_path, splits=splits)
    assert without_ephemeral(live) == opened
