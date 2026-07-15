"""Редактор модели: thread-fetch списка (инъекция), ⚠ для не-tool-capable, метка
«текущая» («в файле», если поле перекрыто оверлеем) у модели из конфига,
строка-источник списка, ручной ввод."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from tui.palette import ACCENT, DEEP_DIM
from tui.widgets.paste_input import PasteInput

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
            total = len(models)
            models = self._cap_models(models)
            self._truncated = total - len(models)
        if models and self._active in {m.id for m in models}:
            self._idx = next(i for i, m in enumerate(models) if m.id == self._active)
        self._models = models
        self.models_loaded = True
        for w in self.query("#onb-model-loading"):
            w.remove()
        self._mount_list()
        if self._on_loaded is not None:
            self._on_loaded()

    def _ensure_active(self, models: list) -> list:
        """Инвариант: действующая модель всегда присутствует в списке выбора.

        Сервер может её не вернуть — сменили эндпоинт, модель снята с раздачи. Без неё
        предвыбор встаёт на чужую модель: секция читается как «изменено» при входе, а
        Enter молча подменяет модель в конфиге. _cap_models держит тот же инвариант при
        обрезке длинных списков; здесь он же для случая «модели нет в ответе сервера».
        """
        if not self._active or self._active in {m.id for m in models}:
            return models
        from model_picker import ModelInfo
        self._active_offered = False
        return [ModelInfo(id=self._active)] + list(models)

    def _cap_models(self, models: list) -> list:
        """Не более _MODEL_LIMIT моделей в списке (сервер может отдать сотни →
        тормоза и невозможно найти нужную). Действующую модель включаем всегда,
        даже если она за пределами топа — иначе «текущая»/предвыбор сломаются."""
        if len(models) <= _MODEL_LIMIT:
            return models
        top = list(models[:_MODEL_LIMIT])
        if self._active and self._active not in {m.id for m in top}:
            active = next((m for m in models if m.id == self._active), None)
            if active is not None:
                top = top[:_MODEL_LIMIT - 1] + [active]
        return top

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

    def _row_text(self, i: int, m) -> Text:
        cursor = "▸ " if i == self._idx else "  "
        dot = "● " if m.id == self._active else "○ "   # ● — модель из конфига, ○ — прочие
        warn = " ⚠" if m.tool_capable is False else ""
        t = Text()
        t.append(cursor, style=ACCENT)                 # курсор teal, как в меню
        t.append(f"{dot}{m.id}{warn}")
        if m.id == self._active:
            t.append(self._active_mark(), style=DEEP_DIM)
        return t

    def _manual_text(self) -> Text:
        """«вписать вручную…» — тоже вариант выбора (○), с курсором ▸ (teal)."""
        from onboarding_fields import MANUAL_ENTRY
        manual_i = len(self._models or [])
        cursor = "▸ " if self._idx == manual_i else "  "
        t = Text()
        t.append(cursor, style=ACCENT)
        t.append(f"○ {MANUAL_ENTRY}")
        return t

    def _mount_list(self) -> None:
        from onboarding_fields import MODEL_WARN
        models = self._models
        spaced = " set-spaced" if self._framed else ""
        input_cls = "onb-dyn set-framed" if self._framed else "onb-dyn"
        if not models:
            inp = PasteInput(value=self._current, placeholder="имя модели",
                             id=self._input_id, classes=input_cls)
            self.mount(inp)
            self.mount(Static(_SOURCE_FAIL, classes="onb-hint onb-dyn"))
            inp.focus()
            return
        for i, m in enumerate(models):
            cls = "onb-option onb-dyn" + spaced + (" selected" if i == self._idx else "")
            self.mount(Static(self._row_text(i, m), classes=cls))
        if self._truncated > 0:
            self.mount(Static(f"…и ещё {self._truncated} — впишите имя вручную ниже",
                              classes="onb-hint onb-dyn"))
        manual_i = len(models)
        cls = "onb-option onb-dyn" + spaced + (" selected" if self._idx == manual_i else "")
        self.mount(Static(self._manual_text(), classes=cls))
        if any(m.tool_capable is False for m in models):
            self.mount(Static(f"⚠ {MODEL_WARN}", classes="onb-warn onb-dyn"))
        inp = PasteInput(value=self._current, placeholder="имя модели",
                         id=self._input_id, classes=input_cls)
        inp.display = self._idx == manual_i
        self.mount(inp)
        # Строка-источник — В САМОМ НИЗУ (не разбивает варианты выбора и их поле).
        self.mount(Static(_SOURCE_FAIL if self._synthetic else _SOURCE_OK,
                          classes="onb-hint onb-dyn"))

    def move(self, delta: int) -> None:
        models = self._models or []
        n = len(models) + 1
        self._idx = (self._idx + delta) % n
        cur = self._idx
        for i, w in enumerate(self.query(".onb-option")):
            if i < len(models):
                w.update(self._row_text(i, models[i]))
            else:
                w.update(self._manual_text())
            (w.add_class if i == cur else w.remove_class)("selected")
        manual_i = len(models)
        inp = self.query_one(Input)
        inp.display = cur == manual_i
        if cur == manual_i:
            inp.focus()

    @property
    def has_models(self) -> bool:
        return bool(self._models)

    def submit(self) -> str | None:
        """Имя модели или None (не менять)."""
        models = self._models or []
        if not models or self._idx == len(models):
            try:
                raw = self.query_one(Input).value.strip()
            except Exception:
                raw = ""
            return raw or None
        return models[self._idx].id

    def peek(self) -> str | None:
        """Текущий выбор без сайд-эффектов (для dirty-проверки)."""
        return self.submit()
