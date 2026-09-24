"""PostUpdateScreen — пост-апдейт экран (спека D, D4). Показывается на первом
запуске новой версии (см. `upgrade_gate.detect_upgrade`): чеклист `[●]/[ ]`
«обновить подключённые MCP» / «обновить config.yml до текущей версии»,
Space — отметить, Enter — выполнить отмеченное, Esc — позже.

Стамп (`upgrade_gate.mark_current_run`) пишется при закрытии экрана (Enter
ИЛИ Esc — `on_unmount`), не раньше: крэш до закрытия должен повторить показ."""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.palette import ACCENT, DEEP_DIM, DIM, INK, OK
from gc_tui.form_frame import (FOOTER_SCROLL, body_scrollable, cap_frame_height, scroll_body)

_FOOTER = "Space — отметить · Enter — выполнить отмеченное · Esc — позже (доступно в /settings)"
_FOOTER_EMPTY = "Enter или Esc — продолжить"      # отмечать нечего — футер не врёт
_HINT = "Осталось обновить:"


class PostUpdateScreen(Screen):
    # Модальный экран (см. OnboardingScreen.BLOCKS_GLOBAL_NAV): показывается сразу после
    # апгрейда, до того как юзер дошёл до сессии. Гейт — TuiApp.check_action.
    BLOCKS_GLOBAL_NAV = True
    # Миксин здесь ради одного — потолка рамки (Р19): курсор экрана по карточке
    # не ходит, прокрутка у него своя, страницами (PgUp/PgDn контейнера).

    def __init__(self, *, upgrade, config: dict, config_path: str) -> None:
        super().__init__()
        self._upgrade = upgrade
        self._config = config
        self._config_path = config_path

        from gc_core import config_sync as _config_sync
        from recommended_mcp.runtime import cli as _mcp_cli
        self._mcp_cli = _mcp_cli
        self._config_sync = _config_sync

        self._wired = list(self._mcp_cli.wired_names())
        self._needs_sync = self._compute_needs_sync()

        # Пункт MCP появляется, только если кому-то из подключённых правда есть что
        # обновить (версия в рецепте новой версии агента ≠ установленной). Опрос версий —
        # subprocess на сервер, поэтому в фоновом воркере (on_mount), а не в __init__.
        self._mcp_outdated: list[str] = []
        # Голосовой движок — такой же «поставленный рядом» компонент, как MCP: версия
        # приколота в коде агента, и после апгрейда на диске остаётся прежняя. Проверка
        # дешёвая (чтение state.json), поэтому без воркера — в отличие от MCP, где опрос
        # версий это subprocess на сервер.
        self._voice_update = self._compute_voice_update()
        self._items: list[str] = []
        if self._voice_update is not None:
            self._items.append("voice")
        if self._needs_sync:
            self._items.append("config")
        self._marked = set(self._items)   # default-ON
        self._idx = 0
        self._stamped = False

    def on_mount(self) -> None:
        self.call_after_refresh(cap_frame_height, self, "#pu-scroll")
        if self._wired:
            self.run_worker(self._scan_mcp_updates, thread=True, name="post-update-mcp-scan")

    def on_resize(self, event: events.Resize) -> None:
        # Дважды: сразу — под новую высоту экрана, после раскладки — под новую
        # высоту полосы подсказок (на узком окне она переносится и растёт: своего
        # `Resize` при этом нет, и одиночный замер остался бы протухшим).
        cap_frame_height(self, "#pu-scroll")
        self.call_after_refresh(cap_frame_height, self, "#pu-scroll")
        # Влезло ли содержимое, видно только ПОСЛЕ раскладки — от этого зависит,
        # называть ли клавишу прокрутки.
        self.call_after_refresh(self._sync_footer)

    def _scan_mcp_updates(self) -> None:
        """Фоновый воркер: у кого из подключённых версия отстала от рецепта."""
        outdated: list[str] = []
        try:
            manifest = self._mcp_cli.load_manifest_entries()
            for name in self._wired:
                if self._mcp_cli.installed_state(name, manifest=manifest).get("update"):
                    outdated.append(name)
        except Exception:
            outdated = []                 # версии не узнали — вслепую переустановку не навязываем
        self.app.call_from_thread(self._apply_mcp_updates, outdated)

    def _apply_mcp_updates(self, outdated: list) -> None:
        if not outdated or not self.is_mounted:
            return
        self._mcp_outdated = outdated
        self._items.insert(0, "mcp")      # MCP первым (config — ниже, §6)
        self._marked.add("mcp")
        self._remount_items()

    # --- расчёт состояния ---

    def _compute_voice_update(self):
        """(стоит, нужно) — если локальный голосовой движок отстал от пина новой версии
        агента; None — обновлять нечего.

        Правило сверки одно на весь продукт и живёт в фасаде (`voice_setup`): его же
        читают строка режима и клавиша обновления в /settings."""
        try:
            from gc_tui.services.voice_setup import engine_update
        except Exception:
            return None
        return engine_update()

    def _compute_needs_sync(self) -> bool:
        from gc_core.config import _default_config_path
        from gc_core.utils.user_text import read_user_text_or_none
        # Экран показывается СРАЗУ после апгрейда, до первой сессии: отказ здесь —
        # это «агент не открылся после обновления», а не «одним пунктом меньше».
        user_text = read_user_text_or_none(self._config_path) or ""
        template_text = read_user_text_or_none(_default_config_path()) or ""
        try:
            return bool(self._config_sync.needs_sync(user_text, template_text))
        except ValueError:
            return False

    # --- хром ---

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with VerticalScroll(id="pu-scroll"):
                with Vertical(id="pu-frame"):          # содержимое рамки (§6, Р19)
                    title_classes = "pu-title" if self._items else "pu-title solo"
                    yield Static(self._title_text(), classes=title_classes, markup=False)
                    # Блок «Осталось обновить» — только когда правда есть что обновлять
                    # (иначе экран сводится к факту обновления, а футер — к «продолжить»).
                    if self._items:
                        yield Static(_HINT, classes="post-update-hint pu-hint")
                    for i, key in enumerate(self._items):
                        yield Static(self._row_text(key, i), classes="post-update-item")
            yield Static(self._footer_text(), id="footer", markup=False)

    def _footer_text(self) -> str:
        base = _FOOTER if self._items else _FOOTER_EMPTY
        # Клавиша прокрутки называется, только пока есть что прокручивать — то
        # же правило, что в настройках и онбординге.
        return base + FOOTER_SCROLL if body_scrollable(self, "#pu-scroll") else base

    def _sync_footer(self) -> None:
        try:
            self.query_one("#footer", Static).update(self._footer_text())
        except Exception:
            pass

    def _title_text(self) -> Text:
        old = self._upgrade.old_version
        new = self._upgrade.new_version
        t = Text()
        t.append("✓ ", style=OK)
        if old and old != new:
            t.append(f"Агент обновлён: {old} → {new}")
        else:
            # версия не изменилась (тот же номер, другой коммит — типично для dev
            # без бампа version в pyproject): «X → X» бессмысленно, но саму версию
            # показываем всегда — юзер должен видеть, на чём он сейчас.
            t.append(f"Агент обновлён · v{new}")
        return t

    def _row_text(self, key: str, i: int) -> Text:
        t = Text()
        t.append("▸" if i == self._idx else " ", style=ACCENT)
        checked = key in self._marked
        t.append("[●] " if checked else "[ ] ", style=INK if checked else DEEP_DIM)
        if key == "mcp":
            t.append("Обновить подключённые MCP")
            t.append("   ")
            t.append(", ".join(self._mcp_outdated), style=DIM)
        elif key == "voice":
            have, want = self._voice_update
            t.append("Обновить голосовой движок")
            t.append("   ")
            t.append(f"{have} → {want}", style=DIM)
        else:
            t.append("Обновить config.yml до текущей версии")
        return t

    def _refresh_rows(self) -> None:
        rows = list(self.query(".post-update-item"))
        for i, (w, key) in enumerate(zip(rows, self._items)):
            w.update(self._row_text(key, i))

    def _remount_items(self) -> None:
        """Список пунктов изменился (пришёл скан MCP) — перемонтировать блок и футер:
        подзаголовок и полный футер появляются вместе с первым пунктом."""
        frame = self.query_one("#pu-frame")
        for w in list(self.query(".post-update-item")) + list(self.query(".pu-hint")):
            w.remove()
        title = self.query_one(".pu-title")
        title.set_class(not self._items, "solo")   # появился пункт → вернуть линию-разделитель
        if self._items:
            frame.mount(Static(_HINT, classes="post-update-hint pu-hint"))
        for i, key in enumerate(self._items):
            frame.mount(Static(self._row_text(key, i), classes="post-update-item"))
        self._sync_footer()
        # Полный футер длиннее и на узком окне занимает больше строк — потолок
        # рамки считается от его фактической высоты, значит пересчитать.
        self.call_after_refresh(cap_frame_height, self, "#pu-scroll")

    # --- навигация/действия ---

    def _move(self, delta: int) -> None:
        if not self._items:
            return
        self._idx = (self._idx + delta) % len(self._items)
        self._refresh_rows()

    def _toggle_mark(self) -> None:
        if not self._items:
            return
        key = self._items[self._idx]
        if key in self._marked:
            self._marked.discard(key)
        else:
            self._marked.add(key)
        self._refresh_rows()

    def _execute(self) -> None:
        # ASCII-only print-маркеры ([ok]/[fail]) — не $ok-глифы (✓/✗): консоль,
        # в которую печатает print() при/без app.suspend, может быть не-UTF8
        # (напр. Windows cp1251), а такие глифы вне её кодовой страницы валят
        # print() с UnicodeEncodeError. Rich-рендер экрана (▸/[●]/✓) — отдельно.
        def _do() -> None:
            if "mcp" in self._marked and self._mcp_outdated:
                print("\nОбновляю MCP: " + ", ".join(self._mcp_outdated) + " ...\n")
                for name in self._mcp_outdated:
                    try:
                        rc = self._mcp_cli.install(name)
                        print(f"[{'ok' if rc == 0 else 'fail'}] {name}")
                    except Exception as exc:
                        print(f"[fail] {name}: {exc}")
            if "voice" in self._marked and self._voice_update is not None:
                have, want = self._voice_update
                print(f"\nОбновляю голосовой движок: {have} -> {want} ...\n")
                from gc_tui.services import voice_setup
                try:
                    ok = voice_setup.update_local(self._config, echo=print)
                    print(f"[{'ok' if ok else 'fail'}] голосовой движок")
                except Exception as exc:
                    print(f"[fail] голосовой движок: {exc}")
            if "config" in self._marked and self._needs_sync:
                try:
                    report = self._config_sync.sync_config(self._config_path)
                    print(f"\n[ok] config.yml обновлён, перенесено: {report.transferred}\n")
                except (ValueError, OSError) as exc:
                    print(f"\n[fail] config.yml не обновлён: {exc}\n")
        try:
            with self.app.suspend():
                _do()
        except Exception:
            _do()
        self._close()

    def _close(self) -> None:
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()
        else:
            self.app.exit()

    def on_unmount(self) -> None:
        if self._stamped:
            return
        self._stamped = True
        from gc_core import upgrade_gate
        upgrade_gate.mark_current_run()

    # --- клавиши ---

    def on_key(self, event: events.Key) -> None:
        if event.key in ("ctrl+up", "ctrl+down"):
            # Постраничная прокрутка тела — как в двух других экранах форм.
            event.stop()
            scroll_body(self, "#pu-scroll", -1 if event.key == "ctrl+up" else 1)
        elif event.key == "down":
            event.stop()
            self._move(1)
        elif event.key == "up":
            event.stop()
            self._move(-1)
        elif event.key == "space":
            event.stop()
            event.prevent_default()
            self._toggle_mark()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            self._execute()
