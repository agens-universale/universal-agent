"""Редактор модели: thread-fetch списка (инъекция), ⚠ для не-tool-capable, метка
«текущая» («в файле», если поле перекрыто оверлеем) у модели из конфига,
строка-источник списка, ручной ввод."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Input, Static

from tui.palette import ACCENT, DEEP_DIM
from tui.widgets.paste_input import PasteInput

from typing import NamedTuple


class _Row(NamedTuple):
    kind: str              # "current" | "manual" | "model"
    model: object = None   # ModelInfo для current/model; None для manual


_SOURCE_OK = "список получен с сервера"
_SOURCE_FAIL = "сервер не отдаёт список моделей"
_MODEL_LIMIT = 20   # сервер может отдать сотни моделей — показываем топ, остальное вручную


class ModelEditor(Vertical):
    def __init__(self, *, current: str, endpoint: str, auth: str | None,
                 verify: bool, fetch, input_id: str = "onb-model",
                 active: str = "", on_loaded=None, framed: bool = False,
                 active_overridden: bool = False) -> None:
        super().__init__(classes="onb-dyn")
        self._framed = framed
        self._active_overridden = active_overridden   # модель из файла перекрыта оверлеем
        self._current = current or ""
        self._endpoint = endpoint
        self._auth = auth
        self._verify = verify
        self._fetch = fetch
        self._input_id = input_id
        self._active = active or ""
        self._on_loaded = on_loaded
        self._models = None
        self._synthetic = False        # псевдо-список из active (сервер список не отдал)
        self._active_offered = True    # действующая модель есть в ответе сервера
        self._truncated = 0            # сколько моделей скрыто капом (для подсказки)
        self.models_loaded = False
        self._idx = 0
        self._rows: list[_Row] = []     # кэш видимых строк (пересобирается в _render)
        self._filter: str = ""          # текст фильтра из #model-filter (on_input_changed)

    def compose(self) -> ComposeResult:
        yield Static("загрузка моделей…", classes="onb-hint onb-dyn", id="onb-model-loading")

    def on_mount(self) -> None:
        self.run_worker(self._fetch_blocking, thread=True, exclusive=True,
                        name="model-fetch")

    def _fetch_blocking(self) -> None:
        try:
            models = self._fetch(self._endpoint, self._auth, verify=self._verify) \
                if self._endpoint else None
        except Exception:
            models = None
        self.app.call_from_thread(self._handle_fetch_result, models)

    def _handle_fetch_result(self, models) -> None:
        if not self.is_mounted:
            return
        if not models and self._active:
            from model_picker import ModelInfo
            models = [ModelInfo(id=self._active)]
            self._synthetic = True
        if models:
            models = self._ensure_active(models)
        self._models = models
        self.models_loaded = True
        self._rows = self._visible_rows()
        self._idx = self._initial_idx()
        for w in self.query("#onb-model-loading"):
            w.remove()
        self._mount_list()
        for f in self.query("#model-filter"):
            f.focus()
        if self._on_loaded is not None:
            self._on_loaded()

    def _initial_idx(self) -> int:
        for i, row in enumerate(self._rows):
            if row.kind == "current":
                return i
        for i, row in enumerate(self._rows):
            if row.kind == "model":
                return i
        return 0                                 # только manual

    def _ensure_active(self, models: list) -> list:
        """Инвариант: действующая модель всегда присутствует в списке выбора.

        Сервер может её не вернуть — сменили эндпоинт, модель снята с раздачи. Без неё
        предвыбор встаёт на чужую модель: секция читается как «изменено» при входе, а
        Enter молча подменяет модель в конфиге. _visible_rows держит тот же инвариант
        отдельной строкой «текущая», не подверженной капу _other_models; здесь он же
        для случая «модели нет в ответе сервера».
        """
        if not self._active or self._active in {m.id for m in models}:
            return models
        from model_picker import ModelInfo
        self._active_offered = False
        return [ModelInfo(id=self._active)] + list(models)

    def _active_model(self):
        """ModelInfo действующей модели (из _models), или None."""
        if not self._active or not self._models:
            return None
        return next((m for m in self._models if m.id == self._active), None)

    def _other_models(self) -> list:
        """«Остальные» = модели без действующей; при пустом фильтре — с капом."""
        models = self._models or []
        others = [m for m in models if m.id != self._active]
        if self._filter:                       # активный фильтр снимает кап, режет по подстроке
            q = self._filter.lower()
            others = [m for m in others if q in m.id.lower()]
            self._truncated = 0
            return others
        self._truncated = max(0, len(others) - _MODEL_LIMIT)
        return others[:_MODEL_LIMIT]

    def _visible_rows(self) -> list:
        rows: list[_Row] = []
        active = self._active_model()
        if active is not None:
            rows.append(_Row("current", active))
        rows.append(_Row("manual", None))
        rows.extend(_Row("model", m) for m in self._other_models())
        return rows

    def _active_mark(self) -> str:
        """Подпись у модели из конфига.

        Слово выбирается по тому, ДЕЙСТВУЕТ ли она: перекрытую оверлеем звать «текущей»
        нельзя — экран рядом показывает, что действует другая (спека: поле — редактор
        файла, а не табло).

        Уточнение «нет на сервере» — про модель, которой нет в ответе сервера: она в
        списке (чтобы не подменить её молча), но помечена как не предлагаемая. Исключение —
        ссылка ${ENV:VAR}: её и не может быть в ответе сервера, это не отсутствие модели.
        """
        import config_raw
        word = "в файле" if self._active_overridden else "текущая"
        if config_raw.env_refs(self._active):
            return f"  {word} · из переменной окружения"
        if self._active_offered:
            return f"  {word}"
        return f"  {word} · нет на сервере"

    def _row_text_for(self, row, i: int) -> Text:
        cursor = "▸ " if i == self._idx else "  "
        t = Text()
        t.append(cursor, style=ACCENT)
        if row.kind == "manual":
            from onboarding_fields import MANUAL_ENTRY
            t.append(f"○ {MANUAL_ENTRY}")
            return t
        m = row.model
        is_active = row.kind == "current"
        dot = "● " if is_active else "○ "
        warn = " ⚠" if m.tool_capable is False else ""
        t.append(f"{dot}{m.id}{warn}")
        if is_active:
            t.append(self._active_mark(), style=DEEP_DIM)
        return t

    def _mount_list(self) -> None:
        if not self._models:
            input_cls = "onb-dyn set-framed" if self._framed else "onb-dyn"
            inp = PasteInput(value=self._current, placeholder="имя модели",
                             id=self._input_id, classes=input_cls)
            self.mount(inp)
            self.mount(Static(_SOURCE_FAIL, classes="onb-hint onb-dyn"))
            inp.focus()
            return
        self._rows = self._visible_rows()
        row_filter = Horizontal(
            Static("> ", classes="model-filter-prompt", markup=False),
            PasteInput(value=self._filter, placeholder="поиск модели",
                       id="model-filter", classes="model-filter onb-dyn"),
            classes="model-filter-row onb-dyn",
        )
        self.mount(row_filter)
        self._mount_rows_after_filter()

    def _mount_rows_after_filter(self, from_filter: bool = False) -> None:
        from onboarding_fields import MODEL_WARN
        spaced = " set-spaced" if self._framed else ""
        for i, row in enumerate(self._rows):
            cls = "onb-option onb-dyn" + spaced + (" selected" if i == self._idx else "")
            self.mount(Static(self._row_text_for(row, i), classes=cls))
        if self._truncated > 0:
            self.mount(Static(f"…и ещё {self._truncated} — уточните фильтром",
                              classes="onb-hint onb-dyn"))
        elif self._filter and not any(r.kind == "model" for r in self._rows):
            self.mount(Static("ничего не найдено — впишите вручную",
                              classes="onb-hint onb-dyn"))
        input_cls = "onb-dyn set-framed" if self._framed else "onb-dyn"
        inp = PasteInput(value=self._current, placeholder="имя модели",
                         id=self._input_id, classes=input_cls)
        # При фильтрации (from_filter) ручной ввод не всплывает, даже если _idx сел на
        # manual из-за схлопывания списка под фильтром: амбер-поле активирует только
        # ↑↓-навигация (move), а не побочный эффект сужения — иначе поле зовёт печатать,
        # а клавиши уходят в фильтр (пользователь печатал там).
        inp.display = (not from_filter) and self._rows and self._rows[self._idx].kind == "manual"
        self.mount(inp)
        if any(r.kind in ("current", "model") and r.model is not None
               and r.model.tool_capable is False for r in self._rows):
            self.mount(Static(f"⚠ {MODEL_WARN}", classes="onb-warn onb-dyn"))
        self.mount(Static(_SOURCE_FAIL if self._synthetic else _SOURCE_OK,
                          classes="onb-hint onb-dyn"))
        if from_filter:                          # пользователь печатает в фильтре — фокус там
            for f in self.query("#model-filter"):
                f.focus()

    def _manual_selector(self) -> str:
        return f"#{self._input_id}"

    async def on_input_changed(self, event) -> None:
        if event.input.id != "model-filter":
            return
        new = event.value.strip()
        if new == self._filter:
            return
        self._filter = new
        await self._rebuild_rows()

    async def _rebuild_rows(self) -> None:
        """Пересобрать видимые строки под текущий фильтр, сохранив поле фильтра и фокус.

        Снос старых виджетов дожидается завершения (await): .remove() в Textual лишь
        планирует удаление (реальный detach происходит позже, асинхронно) — без await
        ручной Input того же id ещё не освобождён, когда _mount_rows_after_filter()
        монтирует новый с тем же id → DuplicateIds.
        """
        prev = self._rows[self._idx] if self._rows and 0 <= self._idx < len(self._rows) else None
        await self.query(".onb-option").remove()
        for w in list(self.query(".onb-hint.onb-dyn")):
            if w.id != "onb-model-loading":
                await w.remove()
        await self.query(".onb-warn").remove()
        try:
            await self.query_one(self._manual_selector()).remove()
        except Exception:
            pass
        self._rows = self._visible_rows()
        self._idx = self._reselect_idx(prev)
        self._mount_rows_after_filter(from_filter=True)

    def _reselect_idx(self, prev) -> int:
        """Сохранить выбранную строку по identity через пересбор; пропала → первая видимая (§4.4).

        Клэмп старого числового индекса к новому списку молча подменял бы модель на ту,
        что заняла ту же позицию после фильтрации, — submit()/peek() вернули бы чужой выбор.
        """
        if prev is None:
            return 0
        if prev.kind in ("current", "manual"):
            return next((i for i, r in enumerate(self._rows) if r.kind == prev.kind), 0)
        return next((i for i, r in enumerate(self._rows)
                     if r.kind == "model" and r.model.id == prev.model.id), 0)

    def move(self, delta: int) -> None:
        if not self._rows:
            return
        n = len(self._rows)
        self._idx = (self._idx + delta) % n
        cur = self._idx
        for i, w in enumerate(self.query(".onb-option")):
            w.update(self._row_text_for(self._rows[i], i))
            (w.add_class if i == cur else w.remove_class)("selected")
        inp = self.query_one(self._manual_selector(), Input)
        manual = self._rows[cur].kind == "manual"
        inp.display = manual
        if manual:
            inp.focus()
        else:
            for f in self.query("#model-filter"):
                f.focus()

    @property
    def has_models(self) -> bool:
        return bool(self._models)

    def submit(self) -> str | None:
        """Имя модели или None (не менять)."""
        if not self._rows:                       # сервер без списка → только ручной ввод
            return self._manual_value()
        row = self._rows[self._idx]
        if row.kind == "manual":
            return self._manual_value()
        return row.model.id

    def _manual_value(self) -> str | None:
        try:
            return self.query_one(self._manual_selector(), Input).value.strip() or None
        except Exception:
            return None

    def peek(self) -> str | None:
        """Текущий выбор без сайд-эффектов (для dirty-проверки)."""
        return self.submit()
