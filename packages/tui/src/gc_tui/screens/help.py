"""HelpScreen — справка из реестра команд + доп. хоткеи."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.commands import build_default_registry

# Константы вместо повторяющихся литералов (Sonar S1192).
_LABEL_CTRL_V = "Ctrl+V"


# Ширины боковых колонок строки справки — те же, что в styles.tcss
# (`.help-row-cmd`, `.help-row-key`): по ним считается, сколько остаётся описанию.
_CMD_COL_W = 18
_KEY_COL_W = 13
# Отступы вокруг колонок: padding строки (`.help-row` 0 1) и контейнера (`#trace` 0 1).
_ROW_CHROME_W = 4

# Мини-туториал сверху Help (канон: docs_md/tui/mini-tutorial/SPEC.md).
# (команда/клавиша, пояснение) — две колонки рядом.
_QUICKSTART_HELP: tuple[tuple[str, str], ...] = (
    (
        "F6",
        "Настройки - URL, токен, модель — /settings (MCP при необходимости)",
    ),
    (
        "Enter",
        "Работа - опишите задачу в поле ввода и начните сессию (Enter) — сессия создастся сама",
    ),
    (
        "Ctrl+T",
        "Голосовой ввод - надиктуйте сообщение вместо набора; сервис настраивается в /settings",
    ),
    (
        "F2",
        "Список диалогов. Выберите диалог через ↑ / ↓ и продолжите сессию (Enter)",
    ),
    ("/new или Ctrl+N", "Начать новый диалог"),
    ("F3", "Диффы правок агента — принять или оменить"),
    ("/view <path>", "Постраничный просмотр файла"),
    ("! git status", "Локальный shell - исполнение консольных команд"),
    ("Esc", "Назад / отмена / выход — зависит от контекста"),
    (
        "/ или .",
        "Палитра команд - выберите команду и нажмите Enter или ввыедите команду в поле ввода",
    ),
    ("@", "Упомянуть файл в сообщении"),
    (
        "мышь или Ctrl+C",
        "Копировать в буфер приложения: выделил — сразу в буфере",
    ),
    (
        _LABEL_CTRL_V,
        "Вставка: 1× — плашка «N строк / символов»; 2× тем же текстом — показать целиком",
    ),
    ("Ctrl+C", "Стоп агента (если нет выделения)"),
    ("Ctrl+J", "Перенос строки в поле ввода"),
    ("Ctrl+клик", "Просмотр аргументов и результата инструмента / вызова"),
    ("[asking]", "Агент задал вопрос — напишите ответ в поле и Enter"),
)

# Хоткеи, которых нет у отдельной /команды (F1–F6 / Ctrl+… уже в реестре).
# Перенос строки: Ctrl+J надёжен везде; Shift/Ctrl+Enter — только если терминал
# отдаёт отдельную последовательность (Kitty / modifyOtherKeys).
_EXTRA_HOTKEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "Отправить сообщение"),
    ("Esc", "Назад / отмена / выход — зависит от контекста"),
    ("Ctrl+J", "Перенос строки"),
    ("Ctrl+T", "Голосовой ввод — надиктовать сообщение"),
    ("Ctrl+A", "Выделить всё в поле ввода"),
    (
        _LABEL_CTRL_V,
        "Вставка: 1× — плашка со счётчиком строк/символов; 2× тем же — развернуть целиком",
    ),
    ("мышь", "В трейсе: выделил — сразу в буфер обмена"),
    (
        "Ctrl+Shift+C",
        "То же по клавише; в отличие от Ctrl+C не стопает агента",
    ),
    ("↑ / ↓", "История ввода"),
    ("@", "Упомянуть файл в сообщении"),
    (".", "Палитра команд (как /, без смены раскладки)"),
    ("F6", "Настройки"),
    ("Ctrl+R", "Переименовать активную сессию (пустое → авто-имя)"),
)

# Буфер обмена — не slash-команда; короткая секция в /help.
_CLIPBOARD_HELP: tuple[tuple[str, str], ...] = (
    (
        "Linux",
        "Поставьте wl-clipboard или xclip, чтобы вставлять в другие программы",
    ),
    (
        "Без утилит",
        "Вставьте в поле ввода текста (Ctrl+V), выделите с Shift+мышь, скопируйте хоткеем терминала (Ctrl+Shift+C)",
    ),
    ("Вставка", "Вставить из внешнего буфера обмена на Linux - Ctrl+Shift+V"),
)

# Буфер сессии (B10) — /buffer попадает в «команды» из реестра, но без явной
# секции пользователь не видит, что есть три буфера, что Esc сохраняет, а
# Ctrl+C дописывает в буфер сессии. Синхрон с docs/tui.md.
_BUFFER_SESSION_HELP: tuple[tuple[str, str], ...] = (
    (
        "Три буфера",
        "TUI (copy-on-select) · сессии (накапливающий) · ОС (системный) — в каждой сессии свой",
    ),
    ("F7 / /buffer", "Открыть буфер сессии — большой текст собранных копий"),
    (
        "Esc",
        "Сохранить и закрыть (пустой текст = очистили буфер; без изменений — на диск не пишем)",
    ),
    (
        "Ctrl+C",
        "Копирует выделение наружу и дописывает сверху в буфер сессии (без выделения — стоп агента)",
    ),
    (
        _LABEL_CTRL_V,
        "Вставка на экране буфера — сначала из ОС, пусто — из внутреннего TUI",
    ),
    ("лимит", "20 000 символов; старые куски выкидываются снизу целиком"),
)

# Счётчик токенов и денег в футере. Маркеры «+», «~» и «?» — единственные символы
# интерфейса, смысл которых нельзя угадать, поэтому расшифровка живёт здесь.
# Синхрон с docs/usage_pricing.md и docs/tui.md.
_USAGE_HELP: tuple[tuple[str, str], ...] = (
    (
        "запрос / сессия",
        "Справа в футере: токены и деньги за последний ход и за весь диалог",
    ),
    (
        "откуда цена",
        "Если провайдер сообщает стоимость (OpenRouter, routerai) — берётся она; иначе считается по ставкам из секции pricing (F5/config)",
    ),
    (
        "валюта",
        "Определяется по адресу провайдера в момент вызова: домен .ru — рубли, остальные — доллары. Сменили провайдера посреди диалога — прежние деньги останутся в своей валюте",
    ),
    (
        "+",
        "Соединяет слагаемые суммы: «2.00$+200.0₽» — два доллара и ещё двести рублей. Разные валюты не складываются",
    ),
    ("~", "Часть суммы посчитана по вашим ставкам, а не измерена провайдером"),
    (
        "?",
        "Есть вызовы, стоимость которых показать неоткуда: провайдер цену не сообщил, ставки не заданы. Полный расклад — в логе прогона",
    ),
    ("<0.01", "Сумма меньше минимальной денежной единицы — это не ноль"),
    (
        "только токены",
        "Провайдер цену не сообщает и ставки не заданы — стоимость показать неоткуда",
    ),
)

# Shell (`!`) — не slash-команда; отдельная секция в /help.
_SHELL_HELP: tuple[tuple[str, str], ...] = (
    ("! git status", "Локальный shell (sh/cmd), cwd TUI; Esc/Ctrl+C — отмена"),
    ("! clear [N]", "Убрать bang-вывод из трейса (все или последние N)"),
    ("—", "Без sudo/интерактива (vim, docker -it); вывод только в трейсе"),
)


class HelpScreen(Screen):
    """Справочный экран — колонки «команда | пояснение», блоки с разделителями."""

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("справка", id="header-left", markup=False)
            # Не фокусируем список: иначе срабатывает #trace:focus { outline },
            # и outline ложится поверх первой строки (выглядит как разорванная
            # рамка-заголовок). Прокрутка мышью остаётся; список короткий.
            trace = VerticalScroll(id="trace")
            trace.can_focus = False
            yield trace
            yield Static("[Esc] назад", id="footer", markup=False)

    def on_mount(self) -> None:
        reg = build_default_registry()
        trace = self.query_one("#trace", VerticalScroll)
        row_i = 0

        row_i = self._mount_section(trace, "как работать", row_i)
        for cmd, desc in _QUICKSTART_HELP:
            self._mount_row(trace, row_i, cmd, desc)
            row_i += 1

        from gc_tui.toggles import owning_preset_name

        row_i = self._mount_section(trace, "команды", row_i)
        rows = []
        for cmd in reg.list_commands():
            parts: list[str] = []
            if cmd.args:
                parts.append(cmd.args.strip())
            parts.append(cmd.description)
            # Дочерний тоггл пресета (/gate,/answers) — связь с родителем словом,
            # а не цветом (серый в плоском каталоге читался бы как «выключено»).
            owner = owning_preset_name(cmd.name)
            if owner is not None:
                parts.append(f"настройка /{owner}")
            # Хоткей — НЕ в описание: он идёт своей колонкой (см. ниже).
            rows.append(
                (f"/{cmd.name}", "  ·  ".join(parts), cmd.hotkey or "")
            )
        # Колонка хоткеев прижата к описаниям, а не к правому краю: через пол-экрана
        # пустоты строку глазами уже не собрать. Ширина — по самому длинному описанию
        # ЛЮБОЙ строки. Раньше считалось только по строкам С ХОТКЕЕМ, чтобы не раздувать
        # строку до 104 колонок; расчёт был на то, что длинное описание без клавиши
        # «просто заедет в пустую колонку». Живая проверка показала, что это неправда:
        # колонка описаний фиксированной ширины, и такие строки (/rigor, /gate,
        # /answers — их хвост «настройка /rigor» доходит до 69 знаков) ЛОМАЮТСЯ ПОПОЛАМ,
        # занимая по две строки. Узкий терминал прикрыт потолком `room` ниже — там
        # ограничение и так срабатывало раньше этого максимума.
        # +4, а не +3: Textual считает width ВМЕСТЕ с padding колонки (0 1), и одна
        # колонка уходит на правый отступ — на экране остаются 3 символа, столько же,
        # сколько перед колонкой описаний.
        desc_w = max((len(desc) for _, desc, _ in rows), default=0) + 4
        # И потолок по реальной ширине окна: на узком терминале даже эта колонка
        # выталкивает хоткеи за экран. Вычитаем колонку имени, колонку хоткея и
        # отступы строки; ниже 20 не опускаемся — описание должно оставаться читаемым.
        room = self.size.width - _CMD_COL_W - _KEY_COL_W - _ROW_CHROME_W
        desc_w = max(20, min(desc_w, room))
        for label, desc, hotkey in rows:
            self._mount_row(trace, row_i, label, desc, hotkey, desc_w=desc_w)
            row_i += 1

        row_i = self._mount_section(trace, "Shell (!)", row_i)
        for label, desc in _SHELL_HELP:
            self._mount_row(trace, row_i, label, desc)
            row_i += 1

        row_i = self._mount_section(trace, "хоткеи", row_i)
        for key, desc in _EXTRA_HOTKEYS:
            self._mount_row(trace, row_i, key, desc)
            row_i += 1

        row_i = self._mount_section(trace, "токены и стоимость", row_i)
        for label, desc in _USAGE_HELP:
            self._mount_row(trace, row_i, label, desc)
            row_i += 1

        row_i = self._mount_section(trace, "буфер сессии", row_i)
        for label, desc in _BUFFER_SESSION_HELP:
            self._mount_row(trace, row_i, label, desc)
            row_i += 1

        row_i = self._mount_section(trace, "буфер обмена Linux", row_i)
        for label, desc in _CLIPBOARD_HELP:
            self._mount_row(trace, row_i, label, desc)
            row_i += 1

    @staticmethod
    def _mount_section(trace: VerticalScroll, title: str, row_i: int) -> int:
        """Заголовок блока + тонкая линия. Возвращает row_i без сдвига зебры."""
        if row_i > 0:
            trace.mount(Static(""))
        trace.mount(
            Static(f"── {title} ──", classes="help-section", markup=False)
        )
        return row_i

    @staticmethod
    def _mount_row(
        trace: VerticalScroll,
        index: int,
        cmd: str,
        desc: str,
        hotkey: str = "",
        desc_w: int | None = None,
    ) -> None:
        # зебра: нечётные строки — с чуть более светлым фоном (читаемость)
        classes = "help-row help-row-alt" if index % 2 else "help-row"
        row = Horizontal(classes=classes)
        trace.mount(row)
        # слева команда/клавиша, сразу справа пояснение (не через весь экран)
        row.mount(Static(cmd, classes="help-row-cmd", markup=False))
        desc_w_widget = Static(desc, classes="help-row-desc", markup=False)
        if desc_w is not None:
            desc_w_widget.styles.width = (
                desc_w  # фиксируем — иначе 1fr уводит хоткей вправо
            )
        row.mount(desc_w_widget)
        # Хоткей — своей колонкой: в хвосте описаний разной длины он выстраивался
        # лесенкой, и глазами его было не собрать.
        row.mount(Static(hotkey, classes="help-row-key", markup=False))
