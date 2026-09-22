"""Гонка автопробы и пользовательской проверки в секции «Подключение» (Task 5).

Вход в секцию ставит таймер 50мс на автопробу — она идёт через тот же
_start_draft_probe, что и Enter/Tab, и обе делят счётчик поколений _draft_gen.
Если человек жмёт Enter раньше срабатывания таймера, автопроба, стартовавшая
следом, бампает _draft_gen и обесценивает результат пользовательской пробы —
а у неё в on_done висит сохранение. Итог без фикса: Enter нажат, файл не
изменился, молча (гонка предсуществующая, унаследована из develop 444bb20b)."""

import pytest

pytest.importorskip("textual")

import threading
from pathlib import Path

from form_harness import blocking_probe, ok_probe, recording_probe, settings_connection
from gc_testkit.tui_support import open_settings_section, wait_until
from test_settings_screen import _cfg, _fake_probe, _mk_screen, _write_cfg
from textual.widgets import Input, Static


def _probe_ok_fast():
    from gc_tui.services.settings_probe import ProbeResult

    return ProbeResult("ok", 200, None, 10, "сервер доступен")


async def test_enter_before_autoprobe_still_saves(tmp_path):
    """Enter сразу после входа в секцию (раньше срабатывания таймера автопробы)
    обязан сохранить новый адрес, а не быть обесцененным автопробой."""
    from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen

    cfg_file = _write_cfg(tmp_path)
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(
            SettingsScreen(
                config=_cfg(), config_path=str(cfg_file), on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok_fast())
            )
        )
        await pilot.pause()
        scr = app.screen
        await pilot.press("enter")  # секция «Подключение»
        scr.query_one("#conn-url", Input).value = "https://new-host/v1/chat/completions"
        await pilot.press("enter")  # применить — до срабатывания автопробы (50мс)
        await pilot.pause(0.3)  # заведомо дольше таймера — автопроба точно успела бы
        content = cfg_file.read_text(encoding="utf-8")
        assert "https://new-host/v1/chat/completions" in content, (
            "Enter не сохранил: результат пользовательской пробы обесценен автопробой"
        )


async def test_user_probe_cancels_pending_autoprobe():
    """После входа в секцию таймер автопробы взведён; отмена обязана его снимать —
    иначе он выстрелит позже и обесценит более поздний результат пользователя.

    Открываем секцию через open_settings_section (прямой синхронный вызов
    _open_section), а не pilot.press("enter"): монтирование самого редактора
    иногда занимает больше 50мс (см. отчёт задачи), и клавиша могла бы застать
    таймер уже отработавшим — тест проверяет факт постановки, а не гонку."""
    from gc_tui import app as app_mod

    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Подключение")
        assert scr._autoprobe_timer is not None
        scr._cancel_autoprobe()
        assert scr._autoprobe_timer is None


async def test_autoprobe_survives_enter_absorbed_by_presets(tmp_path):
    """Ревью Important: Enter, поглощённый открытым пресет-листом адреса (список
    раскрыт по умолчанию, когда текущий адрес пуст ИЛИ совпадает с одним из
    встроенных пресетов — обычное дело на чистой установке), не должен гасить
    автопробу без замены. Своя проба в этой ветке не стартует (return до
    _start_draft_probe) — если автопроба всё равно снята, вердикт связи не
    появится вовсе, до первого Tab или правки с Enter.

    Адрес в конфиге — РОВНО один из встроенных пресетов (не пустая строка):
    список раскрывается точно так же (canon §2.1 — подсвечивает совпавший),
    но Enter подставляет ТОТ ЖЕ url, что уже стоит в поле — Textual-реактив
    Input.value не шлёт Changed на равное значение, и не примешивается
    отдельная гонка «когда домотает post_message с правкой поля» (она не
    имеет отношения к порядку отмены/непринятия автопробы, который здесь
    проверяется, — см. отчёт задачи, отдельная находка, в скоуп ревью не
    входит).

    Вход в секцию — через open_settings_section (детерминированно, 0мс до
    таймера) — но само монтирование редактора (on_mount → пресет-лист открыт)
    на этой машине НЕ пренебрежимо малое: калибровка (`trace_mount_timing.py`,
    scratchpad) показала разброс 31-141мс на одно только домонтирование —
    больше самого таймера (50мс) уже на первом же pilot.pause(), стабильно
    (Windows message-pump, видимо, гранулярность ~15-30мс на тик). Гонять
    реальный 50мс-таймер против реального монтирования здесь принципиально
    недетерминированно — это свойство среды, не тестируемого кода.

    Поэтому: монтируем редактор БЕЗ давления по времени (сколько потребуется),
    а таймер автопробы взводим заново — тем же вызовом, что и _open_section —
    непосредственно перед Enter, так что у него гарантированно свежий полный
    интервал. Enter шлём напрямую через scr.on_key(...) (тот же код, что и
    реальная клавиша), а не pilot.press — ноль накладных расходов на
    диспетчеризацию, весь запас достаётся собственно проверяемому механизму."""
    from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    from textual import events

    # Адрес ПУСТОЙ: по канону 2026-08-17 список раскрывается только на пустом поле —
    # а тесту нужен именно раскрытый, чтобы Enter был им поглощён. Раньше сюда годился
    # пресетный адрес, но заполненное поле список больше не поднимает.
    cfg_file = _write_cfg(tmp_path, url='""')
    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(
            SettingsScreen(
                config=_cfg(), config_path=str(cfg_file), on_apply=lambda: None, probe_fn=_fake_probe(_probe_ok_fast())
            )
        )
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Подключение")
        await pilot.pause()  # дать редактору домонтироваться (on_mount → пресет-лист открыт);
        # сколько бы это ни заняло — таймер ниже взводится заново, УЖЕ после того, как всё смонтировано
        from gc_tui.editors.connection import ConnectionEditor

        ed = scr.query_one(ConnectionEditor)
        assert ed._presets_ctrl.is_open, "предпосылка теста: список должен быть раскрыт"
        scr._cancel_autoprobe()  # снять то, что могло взвестись/сработать во время монтирования
        scr._autoprobe_timer = scr.set_timer(
            0.05, scr._autorun_connection_probe
        )  # свежий интервал — как в _open_section
        assert scr._autoprobe_timer is not None
        scr.on_key(events.Key("enter", None))  # поглощён списком — не сабмит, до срабатывания автопробы
        await pilot.pause(0.3)  # заведомо дольше таймера
        assert scr._draft_checked is not None, "автопроба погашена без замены — вердикт связи не появился"


async def test_reentering_connection_stops_previous_autoprobe_timer():
    """Ревью Minor: повторный вход в «Подключение» до срабатывания таймера
    первого захода обязан остановить его, а не оставить тикать рядом с новым —
    иначе орфанные Timer-объекты копятся, а момент появления вердикта плывёт
    (данные это не портит — гонку между двумя автопробами закрывает guard
    _autoprobe_timer, — но таймеры копить не должны).

    Между двумя входами нужен настоящий await — повторный _open_section
    пересобирает DOM редактора, а если первый ещё не домонтировался
    (on_mount не успел отработать), пересборка роняет его на середине
    (NoMatches на #conn-token — воспроизводится независимо от этого фикса,
    просто свойство двух _open_section без паузы между ними). Поэтому ждём
    полного монтирования первого захода, а «первый таймер ещё жив» гарантируем
    не гонкой с реальными 50мс (см. test_autoprobe_survives_enter_absorbed_by_presets
    и отчёт задачи — на этой машине один pilot.pause() и сам может занять
    больше 50мс), а тем же переармированием, что и в _open_section, прямо
    перед вторым входом."""
    from gc_tui import app as app_mod

    app = app_mod.TuiApp(config=_cfg())
    async with app.run_test() as pilot:
        app.push_screen(_mk_screen())
        await pilot.pause()
        scr = app.screen
        open_settings_section(scr, "Подключение")
        await pilot.pause()  # дать первому редактору домонтироваться до конца
        from gc_tui.editors.connection import ConnectionEditor

        assert hasattr(scr.query_one(ConnectionEditor), "_presets_ctrl")
        scr._cancel_autoprobe()
        first_timer = scr.set_timer(0.05, scr._autorun_connection_probe)  # свежий, точно живой
        scr._autoprobe_timer = first_timer
        open_settings_section(scr, "Подключение")  # повторный вход, первый таймер точно ещё не сработал
        second_timer = scr._autoprobe_timer
        assert second_timer is not None
        assert second_timer is not first_timer
        assert first_timer._task is None, "старый таймер автопробы не остановлен при повторном входе в секцию"


# --- автопроба при уходе с поля (§5.2) ----------------------------------------


async def test_leaving_field_probes(tmp_path):
    """Уход с поля стрелкой запускает проверку."""
    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        calls.clear()  # забыть автопробу входа в секцию
        scr.query_one("#conn-url", Input).value = "https://new-host/v1/chat/completions"
        await pilot.press("down")  # ушли с поля
        await wait_until(pilot, lambda: bool(calls), "уход с изменённого поля обязан запустить проверку")


async def test_leaving_unchanged_field_does_not_probe(tmp_path):
    """Поля не менялись — запрос не уходит (снимок против дребезга)."""
    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        calls.clear()
        await pilot.press("down")  # ходим, ничего не правя
        await pilot.press("down")
        await pilot.pause(0.3)
        assert not calls, "на неизменных полях проверка слаться не должна"


async def test_user_enter_beats_autoprobe(tmp_path):
    """Enter сразу после ухода с поля: сохранение происходит, автопроба его не
    обесценивает — пользовательское действие старше (спека §5.2).

    Регресс гонки, которую эта ветка уже чинила один раз: обе пробы делят
    _draft_gen, и стартовавшая позже обесценивает результат более ранней, а в
    on_done пользовательской висит сохранение."""
    new_url = "https://enter-wins/v1/chat/completions"
    async with settings_connection(tmp_path, probe=ok_probe()) as (pilot, scr):
        scr.query_one("#conn-url", Input).value = new_url
        await pilot.press("down")  # уход с поля → автопроба
        await pilot.press("enter")  # человек тут же сохраняет
        await wait_until(
            pilot,
            lambda: new_url in Path(scr._config_path).read_text(encoding="utf-8"),
            "Enter не сохранил: результат пользовательской пробы обесценен автопробой",
        )


async def test_enter_reuses_autoprobe_verdict(tmp_path):
    """Решение по «умному Enter» (шаг 5 задачи): автопроба ухода с поля ЗАПОЛНЯЕТ
    _draft_checked, и Enter решает по её вердикту, СВОЕЙ пробы не запуская.

    Вердикт всегда про текущий черновик: любая правка поля бьёт по
    _reset_draft_state (гасит и вердикт, и летящую пробу через _draft_gen), так
    что непустой _draft_checked не может относиться к другим значениям. Вторая
    проба на Enter была бы тем же запросом с тем же ответом — и задерживала бы
    сохранение на сетевой круг.

    Считаем запросы ПО АДРЕСУ из поля: после сохранения хаб пускает свою
    config-пробу (глифы меню, _start_config_probe) — она про сохранённые
    значения, к «умному Enter» отношения не имеет и в счёт не идёт."""
    calls = []
    new_url = "https://reuse-verdict/v1/chat/completions"

    def draft_calls():
        return [c for c in calls if c[0] == new_url]

    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        calls.clear()
        scr.query_one("#conn-url", Input).value = new_url
        await pilot.press("down")
        await wait_until(pilot, lambda: scr._draft_checked is not None, "автопроба ухода с поля осела")
        after_autoprobe = len(draft_calls())
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: new_url in Path(scr._config_path).read_text(encoding="utf-8"),
            "Enter не сохранил по готовому вердикту",
        )
        assert len(draft_calls()) == after_autoprobe, "Enter послал вторую пробу поверх свежего вердикта автопробы"


# --- гонки внутри латентности пробы (медленная проба-фейк) --------------------
#
# Обе гонки ниже живут ровно в окне «запрос ушёл, ответ не пришёл». На мгновенных
# фейках это окно почти нулевое, и никакое число повторных прогонов их не ловит —
# поэтому проба здесь висит, пока тест сам не отпустит (`blocking_probe`).


async def _settle_entry_probe(pilot, scr, gate, calls=None):
    """Отпустить автопробу входа в секцию и дождаться её вердикта, после чего
    снова закрыть ворота: дальше пробы висят под контролем теста."""
    gate.set()
    await wait_until(pilot, lambda: scr._draft_checked is not None, "автопроба входа в секцию осела")
    gate.clear()
    if calls is not None:
        calls.clear()


async def test_two_arrows_within_latency_still_leave_verdict(tmp_path):
    """Обычная ходьба по форме внутри латентности: правим адрес → ↓ (проба ушла,
    строка «проверяю…») → ↓ ещё раз, не дожидаясь ответа.

    Вторая стрелка не должна ронять летящую пробу первой: вердикта по ней уже не
    будет (снимок совпал — повтор не уйдёт), и строка застряла бы на «проверяю…»
    навсегда — в самом типичном сценарии §5.2 проверка не дала бы результата
    вовсе, да ещё и врала бы."""
    gate = threading.Event()
    calls = []
    new_url = "https://slow-probe/v1/chat/completions"
    async with settings_connection(tmp_path, probe=blocking_probe(gate, calls), wait_probe=False) as (pilot, scr):
        await _settle_entry_probe(pilot, scr, gate, calls)
        scr.query_one("#conn-url", Input).value = new_url
        await pilot.press("down")  # уход с поля → проба ВИСИТ
        await wait_until(pilot, lambda: bool(calls), "проба ухода с поля вылетела")
        assert scr._draft_checked is None, "предпосылка: ответа ещё нет"
        await pilot.press("down")  # вторая стрелка внутри латентности
        await pilot.pause()
        gate.set()  # сервер наконец ответил
        await wait_until(
            pilot, lambda: scr._draft_checked is not None, "вердикт потерян: вторая стрелка обесценила летящую пробу"
        )
        line = str(scr.query_one(".set-probe", Static).render())
        assert "проверяю" not in line, f"строка вердикта застряла: {line!r}"


async def test_arrow_during_enter_probe_keeps_save(tmp_path):
    """Стрелка во время пробы, запущенной Enter, не должна съедать сохранение.

    Когда вердикта нет, Enter вешает сохранение на on_done летящей пробы. Любой
    бамп _draft_gen до её ответа — и on_done не вызывается: файл не изменился,
    молча. Это тот же симптом «нажал Enter, а не сохранилось», против которого
    заведён _cancel_autoprobe, только со стороны стрелки."""
    gate = threading.Event()
    calls = []
    new_url = "https://enter-then-arrow/v1/chat/completions"
    async with settings_connection(tmp_path, probe=blocking_probe(gate, calls), wait_probe=False) as (pilot, scr):
        await _settle_entry_probe(pilot, scr, gate, calls)
        scr.query_one("#conn-url", Input).value = new_url  # правка гасит вердикт
        await pilot.press("enter")  # Enter без вердикта → своя проба + on_done
        await wait_until(pilot, lambda: bool(calls), "проба Enter вылетела")
        await pilot.press("down")  # стрелка внутри латентности
        await pilot.pause()
        gate.set()
        await wait_until(
            pilot,
            lambda: new_url in Path(scr._config_path).read_text(encoding="utf-8"),
            "стрелка во время пробы Enter съела сохранение",
        )


async def test_leaving_field_without_address_keeps_pending_autoprobe(tmp_path):
    """Адреса нет ни в поле, ни в конфиге: своя проба не стартует (_start_draft_probe
    выходит до бампа поколения) — значит и таймер автопробы входа гасить нельзя.

    Ровно тот путь, который запрещает докстринг _cancel_autoprobe: отмена без
    замены оставила бы связь непроверенной вовсе. Задевает чистую установку, где
    адрес и есть пустой.

    Уходим с ТОКЕНА, а не с адреса: при пустом адресе пресет-лист раскрыт, и уход
    с поля адреса не дошёл бы до проверяемого места — его раньше отсёк бы гейт
    «стрелка водила по списку»."""
    from gc_tui import app as app_mod
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.screens.settings import SettingsScreen

    calls = []
    cfg_file = _write_cfg(tmp_path, url='""')
    app = app_mod.TuiApp(config={})  # ни поля, ни действующего адреса
    async with app.run_test() as pilot:
        scr = SettingsScreen(
            config={}, config_path=str(cfg_file), on_apply=lambda: None, probe_fn=recording_probe(calls)
        )
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()  # дать редактору домонтироваться
        ed = scr.query_one(ConnectionEditor)
        scr._cancel_autoprobe()  # снять то, что могло взвестись при монтировании
        scr._autoprobe_timer = scr.set_timer(0.05, scr._autorun_connection_probe)
        assert scr._last_probed_snapshot is None, "предпосылка: снимка нет — гейт дребезга тест не спасает"
        scr._autoprobe_on_leave(ed, "conn-token")
        assert scr._autoprobe_timer is not None, "автопроба входа погашена на пути, где своя проба не стартует"
        assert not calls, "проверять нечего — запрос уходить не должен"
