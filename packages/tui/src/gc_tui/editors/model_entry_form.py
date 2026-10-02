"""ModelEntryForm — форма ОДНОЙ записи реестра `models.<alias>` (профили, волна B).

Грамматика формы взята у `ConnectionEditor` целиком (контракт волны B, п.4):
подпись-`Static` над полем, `PasteInput` с рамкой, поле со списком — внутрь
рамки-группы (`.field-group`), позиционный курсор (`move`) с `None`-элементом на
строке-галке, секрет НЕ предзаполняется — действующий показан маской в
placeholder, контракт наружу — `peek()`/`is_dirty()`/`submit()`.

Форма НЕ пишет никуда и цели записи не знает: `submit()` отдаёт только те поля,
которые заданы явно, а merge со старой записью (и сохранность `pricing`/`backend`,
о которых форма не подозревает) — забота писателя `config_modules/registry_write`.
Пустое поле поэтому и значит «ключ не трогаем»: наследование от основного конфига
показано ЯВНО — placeholder называет конкретное значение, которое унаследуется
(токен — маской), а пресет-лист адреса первым пунктом предлагает «основной
конфиг», Enter по которому осознанно оставляет поле пустым (живая связь).

Две РАЗНЫЕ клавиши, и путать их нельзя (находка A4 ревью плана):
`Ctrl+Space` — проба соединения (тот же differential-вердикт, что в «Подключении»,
и тем же общим `tui/services/settings_probe.probe_connection`); `Alt+↓` — подбор
списка моделей с эндпоинта (сетевой источник строк, механика
`ModelEditor`: воркер-поток + поколение загрузки). Роли не пересекаются: проба
ничего не подбирает, подбор ничего не проверяет.
"""

from __future__ import annotations

from types import SimpleNamespace

from gc_core import onboarding
from gc_core.config_modules.model_registry import (
    connection_parts,
    parse_completion_kwargs_text,
    resolve_entry_address,
    validate_model_alias,
)
from gc_core.onboarding_fields import (
    ADDRESS_HINT as _ADDRESS_HINT,
)
from gc_core.onboarding_fields import (
    NO_AUTH_FROZEN_PLACEHOLDER as _NO_AUTH_FROZEN_PLACEHOLDER,
)
from gc_core.onboarding_fields import (
    NO_AUTH_LABEL as _NO_AUTH_LABEL,
)
from gc_core.onboarding_fields import (
    NO_AUTH_TOGGLE_HINT as _NO_AUTH_TOGGLE_HINT,
)
from gc_core.onboarding_fields import (
    URL_ERROR as _URL_ERROR,
)
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Input, Static, TextArea

from gc_tui.editors.connection import PRESET_NO_MATCH_CLOSED
from gc_tui.editors.field_list import (
    LIST_EMPTY,
    LIST_FAILED,
    LIST_LOADING,
    MODEL_RELOAD_HINT,
    FieldListController,
    write_field,
)
from gc_tui.editors.preset_list import PRESET_HINT as _PRESET_HINT
from gc_tui.editors.preset_list import preset_list_controller
from gc_tui.palette import ACCENT, DEEP_DIM, WARN
from gc_tui.services.probe_presentation import (
    connection_line_text,
    plural_models,
)
from gc_tui.services.yaml_view import safe_yaml_dump
from gc_tui.widgets.paste_input import PasteInput

# Константы вместо повторяющихся литералов (Sonar S1192).
_CLS_ONB_HINT_ONB_DYN = "onb-hint onb-dyn"
_CLS_ONB_HINT_ONB_DYN_SET_GROUP_GAP = "onb-hint onb-dyn set-group-gap"
_SEL_MEF_NOAUTH = "#mef-noauth"
_SEL_MEF_URL = "#mef-url"


# Хинты списков формы. Клавиши названы канон-словарём (спека 2026-08-06 §2.1):
# «перечитать список» — глагол сетевого источника, и он один в обоих состояниях
# списка, свёрнут тот или раскрыт. Сторож словаря — tests/tui/test_footer_contract.
# «В списке такого нет» — формулировка канона 2026-08-18 (та же, что у пресет-листа
# адреса, `preset_list.PRESET_HINT_NO_MATCH`): обе строки живут в одной форме, и
# звать одно и то же состояние двумя разными словами нельзя.
MODEL_LIST_HINT = "модели с сервера · печать — фильтр/свой id · Enter — подставить · Esc — свернуть"
MODEL_LIST_HINT_NO_MATCH = (
    "в списке такого нет · Enter — оставить свой id · Esc — свернуть"
)


def _list_outcome(models) -> str | None:
    """Чем кончился поход за списком: строка состояния или None (всё хорошо).

    `None` от загрузчика значит «не достучались» (исключение проглочено в
    `_fetch_blocking`), пустой список — «сервер ответил, моделей нет». Состояния
    разные, и слова у них разные — те же, что у голосовой панели."""
    if models is None:
        return LIST_FAILED
    return LIST_EMPTY if not models else None


_ALIAS_LABEL = "Алиас"
_ALIAS_HINT = (
    "короткое имя записи — им пользуются слоты и привязки · пусто = имя из id"
)
# Имя занято записью ОСНОВНОГО конфига (§5 спеки эффективного вида): своей записи
# с таким именем в профиле нет, и общее «уже используется» увело бы от того, что
# на самом деле мешает, — отказ называет, ЧЕМ имя занято.
_ALIAS_TAKEN_BY_BASE = (
    "алиас занят записью основного конфига — выберите другое имя"
)
# Алиас не обязателен (ревизия владельца): пустое поле показывает ПРИЗРАКОМ то
# конкретное имя, что реально возьмётся из id (placeholder = default_alias_for +
# уникализация, следует за полем id), и Enter принимает его без ввода. Пока id
# пуст и выводить не из чего — короткий принцип вместо длинной фразы: объяснение
# живёт в подсказке ПОД полем, а не в самом поле.
_ALIAS_PLACEHOLDER = "имя из id модели"
_ALIAS_OR_MODEL_EMPTY = "укажите id модели или алиас"
# Переименование — не «другая запись»: ссылки уровней и привязок едут следом
# одной транзакцией (см. `_submit_form` секции).
_ALIAS_RENAME_HINT = "переименование обновит уровни и привязки записи"
_MODEL_LABEL = "Провайдерский id модели"
_MAIN_LABEL = "main"
_AUX_LABEL = (
    "auxiliary — всегда отключайте reasoning для корректной работы, "
    "используйте параметры вашей модели"
)

# Канон спеки, секция «Текст подсказки». Один статический блок: в поля ничего
# не подставляет.
KWARGS_HINT = """\
Поля `main` и `auxiliary` — обычный YAML, как в config.yml. Пишется то, что уходит
в запрос, без обёртки `chat_completion_kwargs:` / `completion_kwargs:`. `main` —
диалог и субагенты. `auxiliary` — summary, судьи, finish_gate, intent_scope.
Секции не смешиваются: пустой `auxiliary` не забирает ключи из `main`. Одна и та
же температура и `tool_choice` обычно стоят в обеих секциях. В одно поле не
складывать ключи разных семей: строгий прокси отвечает 400 на чужой ключ.

Ключи `model`, `messages`, `tools`, `max_tokens`, `max_completion_tokens` сюда не
писать — их выставляет рантайм. Лимиты служебных ответов
(`max_tokens`, `judge_max_tokens`) — в `chat_completions.auxiliary_llm`, не здесь.
Пустое поле снимает секцию: запрос берёт глобальный словарь
(`backend.chat_completion_kwargs` для main,
`chat_completions.auxiliary_llm.completion_kwargs` для auxiliary).
Явный `{}` — не слать этих ключей, их выбирает сервер модели.

Сэмплинг, общий для семей. У части моделей OpenAI (например gpt-5.5) `temperature`
принимает только 1. `top_k`, `min_p`, `repetition_penalty` — vLLM и похожие, не
каждый прокси.

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
# top_k: 20
# min_p: 0.0
# repetition_penalty: 1.2
```

Qwen / vLLM. Выключить рассуждение:

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
separate_reasoning: false
chat_template_kwargs:
  enable_thinking: false
```

Включить:

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
separate_reasoning: true
chat_template_kwargs:
  enable_thinking: true
```

GLM / DeepSeek-style. Выключить:

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
chat_template_kwargs:
  thinking: false
  reasoning_effort: none
```

Слабее и сильнее:

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
chat_template_kwargs:
  thinking: true
  reasoning_effort: low
```

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
chat_template_kwargs:
  thinking: true
  reasoning_effort: high
```

Если прокси ждёт `reasoning_effort` рядом с `temperature`, а не внутри
`chat_template_kwargs`:

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
reasoning_effort: high
chat_template_kwargs:
  thinking: true
```

OpenAI-like. Выключить и уровни усилия — ключ на том же уровне, что `temperature`:

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
reasoning_effort: none
```

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
reasoning_effort: low
```

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
reasoning_effort: medium
```

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
reasoning_effort: high
```

Модель без thinking API — только сэмплинг, без флагов рассуждения:

```yaml
temperature: 1.0
top_p: 1.0
tool_choice: auto
```

`auxiliary` той же модели повторяет сэмплинг и гасит рассуждение тем диалектом,
который понимает этот бэкенд. Для Qwen это `enable_thinking: false`, для GLM —
`thinking: false` и `reasoning_effort: none`, для OpenAI — `reasoning_effort: none`.

В корабельных примерах та же форма файла: у модели с thinking в `main` секция
`auxiliary` — off.
"""


_STRIP_CYCLE = ("default", "true", "false")
_STRIP_HINT = (
    "default — глобальный флаг · true — вырезать мысль из истории · "
    "false — копировать скрытое поле · пробел — сменить"
)


def _strip_choice(value) -> str:
    """Значение селектора. Не-bool (ключа нет) — ``default``, в файл не пишется."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "default"


def _completion_field_text(value) -> str:
    """Текст YAML-поля.

    Секции нет — пустая строка (наследование глобального словаря). Явный ``{}``
    остаётся ``{}``: пустой набор ключей, не наследование. Непустой mapping —
    раскрытый YAML (`safe_yaml_dump`), не ``{a: b}``."""
    if value is None:
        return ""
    if value == {}:
        return "{}"
    return safe_yaml_dump(value).rstrip("\n")


_MODEL_PLACEHOLDER = "если не указать — id модели равен алиасу"
# Тёзка — своя запись профиля под именем записи основного конфига: пустое поле id у
# неё значит не «равен алиасу», а `model` ЗАСЛОНЁННОЙ записи (так сливает запись
# загрузчик — профиль поверх основного, поключно). Прежний текст обещал бы имя, которым
# запись не пойдёт.
_MODEL_PLACEHOLDER_BASE = "если не указать — {model} из основного конфига"
_URL_LABEL = "Адрес API-сервера"
# Своё подключение задаётся ЦЕЛИКОМ (спека 2026-08-19 §2): адрес и токен принадлежат
# одному провайдеру, и половина пары либо уносит общий ключ на чужой хост, либо уезжает
# вслед за сменой общего адреса. Состояние пары не переключается отдельным элементом —
# оно ВЫВОДИТСЯ из полей, а строка над ними называет, откуда пара взята (§4).
_PAIR_SHARED = "Адрес и токен: наследуются из раздела «Подключение»"
# Тёзка над записью, несущей СВОЁ подключение: пару без своей она берёт у заслонённой
# записи, а не у раздела — «Подключение» здесь называло бы адрес и токен, которые для
# этой записи не действуют (смок владельца: форма звала раздел, показывая при этом хост
# тени). Судит `_inherits_from_base`; тень без своего подключения источника не меняет.
_PAIR_SHARED_BASE = "Адрес и токен: наследуются из записи основного конфига"
_PAIR_OWN = "Адрес и токен: свои"
# Второе написание пары (§2): она живёт в секции `backend` записи, и форма её НЕ правит
# — ни поля, ни клавиша «вернуть наследование» до неё не достают. Молчать об этом нельзя:
# «свои» без уточнения обещали бы правку теми полями, которые к этой паре отношения не
# имеют, и человеку негде было бы узнать, куда идти.
_PAIR_OWN_BACKEND = (
    "Адрес и токен: свои — заданы секцией backend, правятся в файле"
)
# Поля пары — те, на которых работает «вернуть наследование» (§5). Галка в набор не входит:
# она не поле, и её собственная клавиша (Space) на этой строке уже занята.
_PAIR_FIELDS = ("mef-url", "mef-token")
_TOKEN_SHARED_HINT = "токен приходит из раздела «Подключение»"
# Тот же источник, что у шапки пары (`_PAIR_SHARED_BASE`): две подписи одного состояния
# не имеют права называть два разных источника.
_TOKEN_BASE_HINT = "токен приходит из записи основного конфига"
_TOKEN_NEEDED = "укажите токен для этого адреса"
# `Authorization: ""` и сентинел приведены к None ещё в `settings.py:854-856` — маски в
# этом случае не бывает, а молчать нельзя: поле обязано сказать, что теперь будет.
# Наследование пустого общего токена законно: запись не задаёт ничего — значит наследует
# пару целиком и работает без авторизации.
_COMMON_TOKEN_EMPTY = "общий токен не задан — запросы идут без авторизации"
# Заслонённая запись с `token: ""` («сервер не требует авторизации», §2): тёзка
# наследует ровно это, маскировать нечего — а `***` обещал бы секрет, которого нет.
_BASE_TOKEN_EMPTY = (
    "запись основного конфига без токена — запросы идут без авторизации"
)
# Отказ формы — то же правило §2, что у валидатора конфига, укороченное до одной строки
# формы: имена ключей файла человеку здесь ни к чему, перед ним поля и галка.
_PAIR_NEEDS_TOKEN = "задан свой адрес, но не задан токен — укажите токен или отметьте «без авторизации»"
_PAIR_NEEDS_URL = "задан свой токен, но не задан адрес — укажите адрес или верните наследование"
# Пару разорвала не ошибка в полях, а ссылка, которая не подставится: сказать про
# «не задан адрес» там, где человек забыл `export`, значит послать его искать
# несуществующую опечатку.
_PAIR_ENV_MISSING = (
    "переменная окружения {names} не задана — ссылка подставится "
    "пустой, и запись останется без половины пары «адрес + токен»"
)
_TOKEN_LABEL = "Токен"
# Слова клавиши — ОДНА константа на подпись поля и на футер секции (канон
# `onboarding_fields.URL_ERROR`): списанные заново, они разойдутся на первой
# самостоятельной правке одной из витрин, а обещают они одно и то же действие.
CTRL_U_PAIR = "Ctrl+U — вернуть наследование"
# Ctrl+U называется В ПОДПИСИ ПОЛЯ, а не в футере (канон `ConnectionEditor.
# _token_label`): подсказка нужна там, куда смотрят, как «Space — переключить» у
# галки. Глагол ДРУГОЙ, и это не синонимы: у «Подключения» над токеном слоя нет и
# Ctrl+U там СТИРАЕТ, а у записи реестра над ним есть общее подключение — клавиша
# возвращает к нему ПАРУ целиком (§5), а не один токен: половина пары запрещена
# правилом §2. Обещаем ровно в том состоянии, где клавише есть что возвращать.
_TOKEN_LABEL_INHERIT = f"{_TOKEN_LABEL}   {CTRL_U_PAIR}"
# Первый пункт пресет-листа адреса — не «ещё один адрес», а РЕШЕНИЕ не задавать
# свой: поле остаётся пустым, связь живая (сменится адрес в «Подключении» —
# сменится и здесь). Назван РАЗДЕЛОМ, а не файлом: над профилем наследуется
# действующее «Подключение», а профиль вправе нести свой `backend` — «основной
# конфиг» там врало бы (ревизия владельца по смоку). Адрес рядом с ним часто совпадает с адресом обычного
# пресета, и без слова «наследовать» строка читалась бы дублем: одинаковый url,
# разные последствия — литерал в файле против пустого поля (ревизия владельца
# по смоку).
_INHERIT_PRESET_NAME = "раздел «Подключение» · наследовать"
# Тот же пункт у тёзки над записью со своим подключением: пустое поле возвращает её к
# паре ЗАСЛОНЁННОЙ записи, и пункт зовёт этот источник (адрес рядом — её адрес). Пункт
# и `Ctrl+U` — два входа в одно действие (§5), а шапка пары называет его же: три
# витрины одного состояния не имеют права звать три разных источника.
_INHERIT_PRESET_NAME_BASE = "запись основного конфига · наследовать"
_PROBE_RUNNING = "проверяю…"
_PROBE_NO_URL = "сначала укажите адрес — проверка стучится на него"
_LIST_NO_URL = "сначала укажите адрес — список приходит с него"


def _inherited(value: str) -> str:
    return f"наследуется: {value}"


def entry_has_own_connection(entry) -> bool:
    """Несёт ли СЫРАЯ запись реестра своё подключение — ядровым определением §2
    (`connection_parts`) по обоим написаниям пары: `url`/`token` и
    `backend.completions_url`/`proxy`/`chat_completion_headers.Authorization`. По
    совокупности, как `own_connection` формы: правило пары половин не пропускает.

    ОДИН предикат на всех, кто судит ЗАСЛОНЁННУЮ запись основного конфига под тёзкой:
    форма (шапка пары, подсказки, пункт «наследовать», проба) и хвост строки реестра
    (`models_section._address_cell`). Разойдясь хотя бы во втором написании, они
    назвали бы один источник двумя именами. Не-словарь — запись без ничего."""
    if not isinstance(entry, dict):
        entry = {}
    backend = (
        entry.get("backend") if isinstance(entry.get("backend"), dict) else {}
    )
    headers = (
        backend.get("chat_completion_headers")
        if isinstance(backend.get("chat_completion_headers"), dict)
        else {}
    )
    return any(
        connection_parts(
            url=entry.get("url"),
            token=entry.get("token"),
            completions_url=backend.get("completions_url"),
            proxy=backend.get("proxy"),
            authorization=headers.get("Authorization"),
        )
    )


def _fetch_models_trying_auth(fetch, url: str, auth: str | None, **kwargs):
    """Список моделей: сначала заголовок как есть, при пустом ответе — другая
    форма того же токена (голый ключ ↔ ``Bearer``).

    Пустой ответ первой формы не кэшируется (`make_cached_fetch` пишет только
    непустой список), поэтому вторая форма не залипает на чужом промахе.
    Успех первой формы вторую не вызывает."""
    from gc_core.onboarding import authorization_attempts

    attempts = authorization_attempts(auth) or [auth]
    models = None
    for header in attempts:
        try:
            models = fetch(url, header, **kwargs)
        except Exception:
            models = None
        if models:
            return models
    return models


def _resolved_for_request(
    url: str, auth: str | None
) -> tuple[str, str | None]:
    """Раскрыть `${ENV:VAR}`/`${FILE:...}` перед отправкой — тем же резолвером, что у
    `config.load_config`: сама ссылка не адрес и не токен, и буквальный
    `Bearer ${ENV:TOK}` сервер отклоняет — проба врала «✗ токен отклонён» на исправном
    конфиге, а список не грузился никогда.

    Зовётся ТОЛЬКО из воркеров (эталон — `screens/onboarding._auto_probe`): `${FILE:...}`
    — синхронное чтение с диска, и в UI-потоке оно подвешивало бы интерфейс на каждый
    переход курсора. Наружу — в поля, `peek()`, `submit()` — раскрытое не возвращается:
    в файл обязана уехать ссылка."""
    from gc_core.config_modules.env_interpolation import (
        resolve_env_placeholders,
    )

    return (
        resolve_env_placeholders(url),
        resolve_env_placeholders(auth) if auth else auth,
    )


def _env_missing_line(*raw_values: str | None) -> str:
    """Первая из ссылок `${ENV:…}`, которая НЕ подставится, — строкой; "" — подставятся
    все (запрос осмыслен, идём в сеть).

    Резолв ненайденной переменной даёт "", и под визуально заполненным полем и проба, и
    список соврали бы про сервер: пустой адрес читался бы как «нет соединения», пустой
    ключ — как «✗ токен отклонён (401)». Гейт стоит на ОБОИХ концах связи: токенный
    случай даже чаще адресного — `docs/configuration.md` сам толкает держать токен
    ссылкой, а `profiles.find_literal_tokens` ругает литеральный. Называем переменную
    (эталон — `screens/onboarding._auto_probe`).

    Порядок аргументов = порядок проверки, и он не косметический: адрес идёт первым,
    потому что без него запрос бессмыслен независимо от ключа. Одна строка на обе
    сетевые клавиши и на оба конца: причина у них общая, и разойдись они словами, одно
    и то же состояние читалось бы четырьмя."""
    for raw in raw_values:
        if onboarding.env_ref_status(raw) == onboarding.ENV_REF_MISSING:
            return f"⚠ {onboarding.display_value(raw)}"
    return ""


class ModelEntryForm(Vertical):
    DEFAULT_CSS = """
    ModelEntryForm TextArea.mef-yaml {
        height: 5;
    }
    ModelEntryForm #mef-kwargs-hint-scroll {
        height: 8;
    }
    ModelEntryForm #mef-kwargs-hint {
        height: auto;
    }
    """

    def __init__(
        self,
        *,
        initial: dict | None = None,
        existing_aliases=(),
        fetch=None,
        probe=None,
        presets: list | None = None,
        effective_url: str = "",
        effective_auth: str | None = None,
        verify: bool | str = True,
        config_proxy: str | None = None,
        base_aliases=frozenset(),
        base_entry: dict | None = None,
        interaction_gate=None,
    ) -> None:
        super().__init__(classes="onb-dyn")
        # interaction_gate() -> bool — «хозяин забрал клавиатуру» (пикер
        # default-слота, баннер перезаписи ссылки): клавиши секция гейтит сама
        # (_route_registry_editor), а клик доходит до формы напрямую — без
        # этого он переезжал курсором и раскрывал список поверх живого
        # вопроса (ревью N7, находка 1).
        self._interaction_gate = interaction_gate
        initial = initial or {}
        # Эффективные адрес/Authorization основного конфига — то, что реально
        # унаследует запись с пустыми полями (решение 7 «наследование явно»):
        # ими же ходят проба и подбор списка, пока своих значений у записи нет.
        self._effective_url = effective_url or ""
        self._effective_auth = effective_auth
        # Заслонённая запись основного конфига под ТЕМ ЖЕ именем (`{}` — тени нет).
        # Форма её не правит и в поля не кладёт — значения уехали бы копией в файл
        # профиля (`models_section._open_form`); нужна она ровно затем, чтобы называть
        # источник наследования правильно: пару, адрес и id тёзка берёт у неё, а не у
        # «Подключения», и туда же идут проба и подбор списка. Тень — по алиасу, с
        # которым форма открыта: переименование выводит запись из-под неё, и что
        # действует после, показывает строка реестра.
        self._base0 = base_entry if isinstance(base_entry, dict) else {}
        # Алиасы записей основного конфига: занятое ими имя — не «дубль своей»
        # (своей такой нет), у него свой отказ, и призрак пустого алиаса обязан
        # уникализироваться против ЭФФЕКТИВНОГО набора имён, а не одних своих.
        self._base_aliases = frozenset(base_aliases)
        self._own_alias = initial.get("alias") or None
        self._other_aliases = set(existing_aliases) - (
            {self._own_alias} if self._own_alias else set()
        )
        self._alias0 = self._own_alias or ""
        self._model0 = initial.get("model") or ""
        self._url0 = initial.get("url") or ""
        self._main_text = _completion_field_text(initial.get("main"))
        self._aux_text = _completion_field_text(initial.get("auxiliary"))
        self._strip_think = _strip_choice(initial.get("strip_think"))
        # Второе написание пары (`backend.completions_url` / `proxy` /
        # `chat_completion_headers.Authorization`) форма не правит, но обязана ВИДЕТЬ:
        # правило §2 считает его таким же своим подключением, и запись, задавшую пару
        # так, форма иначе объявила бы общей и отказалась сохранить без единой правки.
        backend0 = initial.get("backend")
        self._backend0 = backend0 if isinstance(backend0, dict) else {}
        # Состояние пары, показанное на экране: DOM трогаем только на его смене.
        self._pair_own: bool | None = None
        self._token_current = initial.get(
            "token"
        )  # только для маски placeholder
        self._no_auth = (
            self._token_current == ""
        )  # явное "" в записи = галка ●
        # Третье состояние поля токена (идиома `ConnectionEditor`): пусто = «ключ не
        # трогаем», напечатано = «заменить», помечено = «снять свой ключ и вернуть
        # наследование». Нужно потому, что токен, в отличие от прочих полей, физически
        # не в Input — он секрет, поле держат пустым, и стирать обычным Backspace
        # там нечего.
        self._token_cleared = False
        self._verify = verify
        self._config_proxy = config_proxy
        if fetch is None:
            from gc_core.model_picker import fetch_models

            from gc_tui.services.model_list_cache import make_cached_fetch

            fetch = make_cached_fetch(fetch_models)
        if probe is None:
            # `probe_auth`, а не `probe`: вердикт об авторизации идёт СТРОГО по
            # chat/completions — у публичного `/models` (OpenRouter) он получился бы
            # ложным. Тот же дефолт, что у «Подключения» (`settings.py`).
            from gc_tui.services.settings_probe import (
                probe_auth as default_probe,
            )

            probe = default_probe
        self._fetch = fetch
        self._probe = probe
        # Позиции курсора — порядок человека, настраивающего подключение (ревизия
        # владельца): куда → чем → что → как назвать. К входу в поле id адрес и
        # токен уже стоят — автоподгрузка списка идёт по ним. Алиас — обычное поле
        # и при правке (переименование — транзакция секции). `None` — строка-галка
        # «без авторизации», как в _positions ConnectionEditor: это не Input,
        # фокус с неё снимается явно.
        self._positions = (
            "mef-url",
            "mef-token",
            None,
            "mef-model",
            "mef-alias",
            "mef-strip",
            "mef-main",
            "mef-auxiliary",
        )
        self._idx = 0
        self._models: list = []
        # Чем кончилась последняя загрузка: None — не ходили или всё хорошо, иначе
        # готовая строка состояния. Пустой `_models` сам по себе двусмыслен — «ещё не
        # ходили», «сервер ответил пусто» и «не достучались» выглядят одинаково, а
        # говорить о них надо разное (`field_list.LIST_FAILED` / `LIST_EMPTY`).
        self._list_status = None
        # Ключ последней автоподгрузки — (url, auth), а не флаг «уже пробовали»:
        # ревизия владельца по смоку. Сбой при пустом токене, потом введённый
        # токен — вход в поле обязан сходить ЗАНОВО, флаг же держал форму на
        # первой неудаче до ручного Alt+↓. Повтор с ТЕМИ ЖЕ параметрами не
        # идёт: дребезга «ходьба по форме долбит сервер» не будет.
        self._autoload_key: tuple | None = None
        self._list_gen = 0  # поколение загрузки: протухший ответ отбрасывается
        self._probe_gen = 0
        # Оба контроллера заводятся ЗДЕСЬ, а не на монтировании: смонтированные поля
        # им нужны только в работе (`_field()` ищет по id лениво), а «ещё не заведён»
        # — состояние, о котором пришлось бы помнить каждому вызову хозяина.
        presets_items = list(presets) if presets is not None else []
        inherited_url = self._inherited_url()
        if inherited_url and not self._pair_in_backend():
            # Пункт-наследование первым и с РЕАЛЬНЫМ унаследованным адресом (тени
            # либо основного конфига — `_inherited_url`): Enter на нём осознанно
            # ОЧИЩАЕТ поле (`_apply_preset`), а пустое поле и значит «наследовать».
            # Запись с собственным адресом — не исключение, наоборот: ей этот путь и
            # нужен, другого явного возврата к наследованию нет (решение 7). Над ЛЮБЫМ
            # заданным адресом — своим или каталожным — список сам не раскрывается
            # (канон 2026-08-17, `can_open`): там его раскрывает `reload_presets`
            # (Alt+↓).
            #
            # Пара во втором написании (секция `backend`) пункта не получает: он и
            # `Ctrl+U` — два входа в ОДНО действие (§5), а форма `backend` не правит,
            # и снятые `url`/`token` оставили бы запись своей. Клавиша в этом состоянии
            # честно отказывает (`_pair_returnable`), пункт же доводил дело до половины
            # пары, которую `submit()` отвергает, — из тупика человеку выйти нечем.
            # Гейт — статическая половина того же предиката: динамическая
            # (`own_connection`) к списку, собранному один раз, неприменима, а при общей
            # паре пункт законен — он держит живую связь с основным конфигом.
            presets_items.insert(
                0,
                SimpleNamespace(
                    name=(
                        _INHERIT_PRESET_NAME_BASE
                        if self._inherits_from_base()
                        else _INHERIT_PRESET_NAME
                    ),
                    url=inherited_url,
                    inherit=True,
                ),
            )
        self._presets_ctrl = preset_list_controller(
            self,
            presets=presets_items,
            row_class="mef-preset-row",
            input_id="mef-url",
            hint=_PRESET_HINT,
            on_pick=self._apply_preset,
        )
        self._list = FieldListController(
            self,
            input_id="mef-model",
            row_class="mef-model-row",
            scroll_class="mef-model-scroll",
            hint_class="mef-model-hint",
            promise_empty=True,
            rows_source=lambda: list(self._models),
            render_row=self._model_row_text,
            matches=lambda m, q: q in m.id.lower(),
            value_of=lambda m: m.id,
            on_pick=self._apply_model,
            # Подсветка на раскрытии — строка, РАВНАЯ значению поля (канон
            # `voice_endpoint._model_initial_index`), и она же держится при фильтре:
            # раскрытие поверх набранного id фильтруется этим же id (`_on_models`),
            # и «первая уцелевшая» подсовывала под Enter соседа `…-turbo` вместо
            # своего (находка M6 ревью MR #80).
            initial_index=self._model_initial_index,
            refilter_index=self._model_refilter_index,
            hint=lambda ok: (
                MODEL_LIST_HINT if ok else MODEL_LIST_HINT_NO_MATCH
            ),
            closed_hint=MODEL_RELOAD_HINT,
            # Канон 2026-08-17: САМ список раскрывается только над пустым полем —
            # поверх набранного id он увёл бы ↑/↓ себе, и Enter молча затёр бы
            # введённое. Готовое значение показывает список только по явной просьбе
            # (`Alt+↓` → `forced=True`), а чем его вызвать, говорит постоянная строка
            # под полем (`MODEL_RELOAD_HINT`, её держит `_sync_hint`).
            can_open=lambda value: not value,
            # Печать в поле id — не только фильтр, но и ввод СВОЕГО имени модели
            # (сервер может её не отдавать): откатывать её по Esc нельзя — правило
            # поля адреса, а не языка (`preset_list`, `restore_on_close=False`).
            restore_on_close=False,
        )
        self._initial_snapshot = {
            "alias": self._alias0,
            "url": self._url0,
            "token": "",
            "model": self._model0,
            "main": self._main_text,
            "auxiliary": self._aux_text,
            "strip_think": self._strip_think,
        }

    # --- рендер ---
    def _is_edit(self) -> bool:
        """Правка существующей записи, а не создание новой. Пустое поле здесь значит
        «оставить прежнее» (merge писателя ключа не тронет), и обещать «наследуется»
        было бы неправдой — placeholder'ы это различают."""
        return self._own_alias is not None

    def _derived_alias(self, model: str) -> str:
        """Имя, которое реально возьмётся при пустом алиасе: логика мастера
        (`default_alias_for` + уникализация против чужих имён).

        Занятыми считаются и алиасы основного конфига (§5): предложить призраком
        имя, которое submit тут же отвергнет, значит соврать полем."""
        return onboarding.unique_alias(
            onboarding.default_alias_for(model),
            self._other_aliases | self._base_aliases,
        )

    def _alias_placeholder(self, model: str) -> str:
        return self._derived_alias(model) if model else _ALIAS_PLACEHOLDER

    def _alias_hint(self) -> str:
        """Подсказка под полем алиаса — по тому, что клавиша здесь реально делает:
        правка → про переезд ссылок; создание → про правило имени."""
        return _ALIAS_RENAME_HINT if self._is_edit() else _ALIAS_HINT

    def _refresh_alias_placeholder(self) -> None:
        """Призрак в поле алиаса следует за полем id: печать в id меняет имя,
        которое возьмётся по умолчанию, — placeholder обязан говорить правду."""
        for w in self.query("#mef-alias"):
            w.placeholder = self._alias_placeholder(self._model_value())

    def _inherits_from_base(self) -> bool:
        """Пару без своей запись берёт у ЗАСЛОНЁННОЙ записи основного конфига: та несёт
        своё подключение (§2, тем же предикатом, каким хвост строки реестра судит
        источник адреса). Нет — источник по-прежнему «Подключение», и все прежние
        слова о нём верны: тень без пары источника пары не меняет."""
        return entry_has_own_connection(self._base0)

    def _inherited_url(self) -> str:
        """Адрес, который запись БЕЗ своего унаследует: `url` заслонённой записи, её
        второе написание `backend.completions_url`, иначе действующий из
        «Подключения» — той же ядровой цепочкой (`resolve_entry_address`), какой
        рантайм резолвит слитую запись. Пусто — наследовать нечего."""
        backend = self._base0.get("backend")
        backend = backend if isinstance(backend, dict) else {}
        return str(
            resolve_entry_address(
                url=self._base0.get("url"),
                completions_url=backend.get("completions_url"),
                inherited=self._effective_url,
            )
            or ""
        )

    def _inherited_auth(self) -> str | None:
        """Authorization, с которым уйдёт запись без своей пары: от заслонённой записи,
        если пара её (`_inherits_from_base`), иначе действующий из «Подключения».

        Собирается так же, как его соберёт загрузчик (`ModelEntry.authorization_header`):
        голый `token` → Bearer, `""` → без авторизации, заголовок из `backend` — как
        есть. Поле, проба и подбор списка обязаны говорить об одной связи — все трое
        спрашивают здесь."""
        if not self._inherits_from_base():
            return self._effective_auth
        token = self._base0.get("token")
        if token is not None:
            return onboarding.authorization_header(token) if token else ""
        backend = self._base0.get("backend")
        backend = backend if isinstance(backend, dict) else {}
        headers = backend.get("chat_completion_headers")
        return (headers if isinstance(headers, dict) else {}).get(
            "Authorization"
        )

    def _model_placeholder(self) -> str:
        """Пустое поле id: провайдерским id служит алиас — либо, у тёзки, `model`
        заслонённой записи (так сливает запись загрузчик). Источник id судится
        отдельно от источника пары: тень без подключения id всё равно даёт."""
        model = self._base0.get("model")
        if isinstance(model, str) and model:
            return _MODEL_PLACEHOLDER_BASE.format(model=model)
        return _MODEL_PLACEHOLDER

    def _url_placeholder(self) -> str:
        """Placeholder поля адреса виден ровно в один момент — когда поле ПУСТО (в
        режиме правки это значит «стёрли, чтобы снять»), поэтому он говорит про
        пустоту, а не про сохранённое: пустое поле = наследование, и текст называет
        конкретный адрес, который начнёт действовать (эталон — маска прокси
        `connection.py:143-145`, где пустое поле тоже значит «снять»).

        Второе написание адреса (`backend.completions_url`) сильнее общего, и при
        пустом поле действует ОНО: назвать призраком общий адрес значило бы назвать
        НЕдействующий. Правило то же, каким поле токена показывает маску своего секрета
        записи, а не унаследованного.

        Что сильнее чего — решает ядровая цепочка (`resolve_entry_address`), та же, что
        у витрины реестра и у рантайма; своего порядка у формы быть не должно. Спрашиваем
        её без `url`: placeholder виден ровно при пустом поле, а собственного `url` в
        записи с адресом в `backend` не бывает — схема запрещает два написания разом.
        Унаследованный адрес — у `_inherited_url`: у тёзки это адрес заслонённой
        записи, и общий здесь тоже был бы НЕдействующим."""
        own_url = resolve_entry_address(
            url=None, completions_url=self._backend0.get("completions_url")
        )
        if own_url:
            return str(own_url)
        inherited = self._inherited_url()
        if inherited:
            return _inherited(inherited)
        return _ADDRESS_HINT

    @staticmethod
    def _as_validator_sees(value):
        """Значение так, как его увидит валидатор конфига: `${ENV:…}` уже подставлены.

        Правило пары судит РАСКРЫТОЕ значение (решение владельца 2026-08-26, вариант Б),
        и форма обязана судить так же. Иначе одну и ту же запись стороны читают
        по-разному: `url: ${ENV:X}` при незаданной переменной форма считала своим
        адресом и требовала токен, а писатель видел пустую строку и требовал токен
        УБРАТЬ — собрать такую запись было нельзя ни одним набором полей.

        ТОЛЬКО `${ENV:…}` — зовётся из `on_input_changed` на КАЖДОЕ нажатие клавиши в
        полях адреса/токена, то есть на UI-потоке; `resolve_env_placeholders` разворачивает
        ещё и `${FILE:...}` синхронным чтением файла с диска — там, где `_resolved_for_request`
        (соседняя форма того же смысла) запрещает это себе прямым текстом. `${FILE:...}`
        остаётся воркерам: правило пары просто видит ссылку как непустую строку — то же
        поведение, какое видело бы поле без единой подстановки вовсе.

        `None` (ключа нет) подстановка не трогает — «токена нет» и «токен пустой» этим
        не путаются."""
        from gc_core.config_modules.env_interpolation import resolve_env_only

        return resolve_env_only(value)

    def _pair_parts(self) -> tuple[bool, bool]:
        """Что запись задаёт СВОЕГО — (адрес, токен) — ядровым определением §2
        (`model_registry.connection_parts`), тем же, каким судит валидатор конфига.

        Считаем то, что получится в файле ПОСЛЕ merge писателя, а не набранное в полях:
        нетронутый токен записи остаётся в ней и без единой правки поля, а галка ● — это
        заданный `token: ""`, не «токена нет». Считать иначе значит разойтись с
        `submit()` — и молча записать то, что правило запрещает: снятая галка над
        сохранённым `token: ""` УДАЛЯЕТ ключ, и пара при своём адресе теряет половину.

        Значения проходят через `_as_validator_sees`: в файл уезжает ссылка, а судится
        то, во что она раскроется."""
        headers = self._backend0.get("chat_completion_headers") or {}
        if self._no_auth:
            token = ""
        elif self._token_value():
            token = self._token_value()
        elif self._token_cleared or self._token_current == "":
            token = None
        else:
            token = self._token_current
        return connection_parts(
            url=self._as_validator_sees(self._url_value()),
            token=self._as_validator_sees(token),
            completions_url=self._as_validator_sees(
                self._backend0.get("completions_url")
            ),
            proxy=self._as_validator_sees(self._backend0.get("proxy")),
            authorization=self._as_validator_sees(
                headers.get("Authorization")
            ),
        )

    def _pair_env_missing(self) -> list[str]:
        """Имена переменных, из-за которых половина пары окажется пустой.

        Спрашивается по ДОРЕЗОЛВНЫМ значениям тех же пяти полей, что судит
        `_pair_parts`: после подстановки от ссылки не остаётся ничего, кроме пустой
        строки, и назвать причину уже нечем."""
        from gc_core.config_raw import missing_env_refs

        headers = self._backend0.get("chat_completion_headers") or {}
        names: list[str] = []
        for value in (
            self._url_value(),
            self._token_value(),
            self._token_current,
            self._backend0.get("completions_url"),
            self._backend0.get("proxy"),
            headers.get("Authorization"),
        ):
            for name in missing_env_refs(value):
                if name not in names:
                    names.append(name)
        return names

    def _pair_in_backend(self) -> bool:
        """Пара (или её половина) задана ВТОРЫМ написанием — секцией `backend` записи.

        Тем же ядровым определением §2, что и всё остальное: спрашиваем его про слой
        `backend` в одиночку, обнулив свои поля. Своей проверки «есть ли ключ в
        backend» здесь быть не должно — она стала бы третьим определением пары."""
        headers = self._backend0.get("chat_completion_headers") or {}
        return any(
            connection_parts(
                url="",
                token=None,
                completions_url=self._as_validator_sees(
                    self._backend0.get("completions_url")
                ),
                proxy=self._as_validator_sees(self._backend0.get("proxy")),
                authorization=self._as_validator_sees(
                    headers.get("Authorization")
                ),
            )
        )

    def _pair_returnable(self) -> bool:
        """Пара своя И форма может вернуть её ЦЕЛИКОМ — условие, по которому клавиша
        работает и по которому её обещают подпись и футер.

        Второе написание исключено: `backend` форма не правит, снятые `url`/`token`
        оставили бы запись своей, и клавиша выглядела бы сделанной, ничего не сделав.
        Обещать полдела — та же ложь витрины, что обещать мёртвую клавишу."""
        return self.own_connection() and not self._pair_in_backend()

    def own_connection(self) -> bool:
        """Пара своя: запись задаёт своим хоть что-то из пары — адрес или токен.
        Токен, галка ● и клавиша «вернуть наследование» живут ровно в этом состоянии.

        По СОВОКУПНОСТИ, а не по одному адресу: стёртый до пустоты адрес — ещё не
        решение (§4, краевые случаи), решением он становится при сохранении. Считай
        форма его решением сразу — оставшаяся половина (свой токен записи) оказалась бы
        в замороженном и недостижимом курсором поле: отказ сохранения виден, а
        исполнить его нечем."""
        return any(self._pair_parts())

    @property
    def token_cleared(self) -> bool:
        """Свой токен записи помечен к снятию (Ctrl+U): `submit()` УДАЛИТ ключ
        `token`, и запись вернётся к общему токену «Подключения». Read-only и без
        сайд-эффектов — то же зеркало третьего состояния, что у `ConnectionEditor`."""
        return self._token_cleared

    def _own_token_active(self) -> bool:
        """У записи есть СВОЙ токен и он ещё в силе: помеченный к снятию (Ctrl+U)
        своим больше не считается — с этого момента и маска, и проба, и submit
        говорят про унаследованный."""
        return bool(self._token_current) and not self._token_cleared

    def _token_is_mask(self) -> bool:
        """placeholder токена сейчас — маска (своего секрета записи или
        унаследованного), а не подсказка: маркер `has-saved` красит её цветом
        значения (спека §3.4), и врать им нельзя."""
        if self._own_token_active():
            return True
        return not self.own_connection() and bool(self._inherited_auth())

    def _token_placeholder(self) -> str:
        """Поле токена говорит ровно то, с чем реально уйдёт запрос, — тем же правилом,
        каким его собирает `_current_auth_header`: свой токен записи — маской; над своим
        адресом без токена — что его надо указать (унаследованный сюда не пойдёт, §4);
        над унаследованным адресом — маска того, что придёт вместе с ним
        (`_inherited_auth`: у тёзки — токен заслонённой записи)."""
        if self._own_token_active():
            return onboarding.display_secret(self._token_current)
        if self.own_connection():
            return _TOKEN_NEEDED
        inherited = self._inherited_auth()
        if inherited:
            return _inherited(onboarding.display_secret(inherited))
        return (
            _BASE_TOKEN_EMPTY
            if self._inherits_from_base()
            else _COMMON_TOKEN_EMPTY
        )

    def _token_label(self) -> str:
        """Подпись поля токена. Клавиша обещается ровно там, где по ней можно нажать и
        где она доделает: пара своя, возвратимая (`_pair_returnable`) и поле живое.

        Гейт по ПАРЕ, а не по своему токену (§5): над своим адресом без токена клавиша
        тоже работает — она и есть выход из половины пары. Под ● поле заморожено и
        курсором пропускается, и обещание в его подписи звало бы туда, куда не встать;
        выход при этом не теряется — с адреса клавиша работает, и там её называет
        футер (канон «недоступное не обещаем»)."""
        return (
            _TOKEN_LABEL_INHERIT
            if self._pair_returnable() and not self._no_auth
            else _TOKEN_LABEL
        )

    def _sync_token_label(self) -> None:
        for w in self.query("#mef-token-label"):
            w.update(self._token_label())

    def _noauth_cls(self) -> str:
        sel = " selected" if self._positions[self._idx] is None else ""
        return "onb-option onb-dyn set-spaced" + sel

    def _noauth_text(self) -> Text:
        # Подсказка живёт В строке, а не в футере: Space — клавиша ровно этой строки,
        # в полях он печатает пробел (разбор подсказок в теле, ConnectionEditor). При
        # общей паре галка неактивна, и строка объясняет почему: обещать «Space —
        # переключить» там, где переключать нечего, — то же враньё, что мёртвая клавиша.
        t = Text()
        t.append(("● " if self._no_auth else "○ ") + _NO_AUTH_LABEL)
        if self.own_connection():
            hint = _NO_AUTH_TOGGLE_HINT
        else:
            hint = (
                _TOKEN_BASE_HINT
                if self._inherits_from_base()
                else _TOKEN_SHARED_HINT
            )
        t.append("   " + hint, style=DEEP_DIM)
        return t

    @staticmethod
    def _model_initial_index(items, value: str) -> int | None:
        """Что подсвечено на раскрытии: строка ДЕЙСТВУЮЩЕГО id (эталоны —
        `preset_list._initial_index`, `voice_endpoint._model_initial_index`).
        Enter сразу после раскрытия не имеет права подменить набранный id первой
        строкой сервера. Своего id в списке нет — подсвеченной строки нет вовсе, и
        Enter оставляет введённое; поле пустое — защищать нечего, первая строка."""
        if not value:
            return 0 if items else None
        value = value.lower()
        return next(
            (i for i, m in enumerate(items) if m.id.lower() == value), None
        )

    @staticmethod
    def _model_refilter_index(items, visible, prev) -> int | None:
        """Куда встаёт ▸ после сужения: та же строка, пока она видна (по identity,
        канон `model._list_refilter_index`). Нужен подсветке значения: раскрытие
        поверх набранного id тут же фильтруется этим id, и умолчание «первая
        уцелевшая» затирало бы выбор `_model_initial_index` соседом по фильтру.

        `prev is None` — ДО этого сужения не было подсвечено ничего ОСОЗНАННО:
        `_model_initial_index` не нашёл строку, РАВНУЮ значению поля (набранного
        id на сервере нет). Фолбэк на первую уцелевшую здесь откатывал бы ровно
        это решение — подсвечивал соседа по фильтру, которого поле не называет
        (M6, находка #13 ревью MR #80: один Enter молча подставлял его). `None`
        держит «ничего не подсвечено» СКВОЗЬ рефильтр, а не только на раскрытии.

        Строка БЫЛА подсвечена (`prev` — не `None`) и ушла из-под фильтра —
        другое дело: первая уцелевшая, как у соседних списков."""
        if prev is None:
            return None
        for pos, i in enumerate(visible):
            if items[i] is prev:
                return pos
        return 0

    def _model_row_text(self, model, *, selected: bool) -> Text:
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(model.id)
        if getattr(model, "tool_capable", None) is False:
            t.append(" ⚠", style=WARN)
        return t

    def compose(self) -> ComposeResult:
        # Рамка одна и она всегда (канон §3.2.1): у поля со списком её рисует
        # группа, у одинокого поля — класс set-framed. Порядок полей — как в
        # `_positions`: адрес → токен → id → алиас.
        icls = "onb-dyn set-framed"
        # Строка состояния пары — НАД парой полей (§4): она про оба сразу, и пометки у
        # каждого поля разнесли бы одно состояние по двум подписям.
        yield Static(
            _PAIR_SHARED, id="mef-pair", classes=_CLS_ONB_HINT_ONB_DYN
        )
        yield Static(_URL_LABEL, classes=_CLS_ONB_HINT_ONB_DYN)
        self._url_input = PasteInput(
            value="",
            placeholder=self._url_placeholder(),
            id="mef-url",
            classes="onb-dyn group-field",
        )
        with Vertical(classes="field-group onb-dyn", id="mef-url-group"):
            yield self._url_input
        yield Static(
            self._token_label(),
            id="mef-token-label",
            classes=_CLS_ONB_HINT_ONB_DYN_SET_GROUP_GAP,
        )
        # has-saved: placeholder говорит одно из двух (маска действующего значения /
        # подсказка-объяснение), и различать их надо не вчитываясь (спека §3.4).
        tcls = icls + (" has-saved" if self._token_is_mask() else "")
        yield PasteInput(
            value="",
            placeholder=self._token_placeholder(),
            password=True,
            id="mef-token",
            classes=tcls,
        )
        yield Static(
            self._noauth_text(), id="mef-noauth", classes=self._noauth_cls()
        )
        yield Static(_MODEL_LABEL, classes=_CLS_ONB_HINT_ONB_DYN_SET_GROUP_GAP)
        # Поле id — со списком моделей (автоподгрузка при входе, Alt+↓ — перечитать),
        # значит внутри рамки-группы: рамка одна на поле и его список (спека §3.1).
        with Vertical(classes="field-group onb-dyn", id="mef-model-group"):
            yield PasteInput(
                value=self._model0,
                placeholder=self._model_placeholder(),
                id="mef-model",
                classes="onb-dyn group-field",
            )
        # Строка состояния списка (загрузка/сбой/счётчик) — пустая не занимает места
        # (`display`), как статусы `ModelEditor`.
        status = Static(
            "", id="mef-model-status", classes=_CLS_ONB_HINT_ONB_DYN
        )
        status.display = False
        yield status
        yield Static(_ALIAS_LABEL, classes=_CLS_ONB_HINT_ONB_DYN_SET_GROUP_GAP)
        yield PasteInput(
            value=self._alias0,
            placeholder=self._alias_placeholder(self._model0),
            id="mef-alias",
            classes=icls,
        )
        yield Static(self._alias_hint(), classes=_CLS_ONB_HINT_ONB_DYN)
        yield Static(
            self._strip_text(),
            id="mef-strip",
            classes="onb-option onb-dyn set-spaced",
            markup=False,
        )
        yield Static(_STRIP_HINT, classes=_CLS_ONB_HINT_ONB_DYN, markup=False)
        yield Static(_MAIN_LABEL, classes=_CLS_ONB_HINT_ONB_DYN_SET_GROUP_GAP)
        yield TextArea(
            self._main_text,
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            id="mef-main",
            classes="mef-yaml",
        )
        yield Static(_AUX_LABEL, classes=_CLS_ONB_HINT_ONB_DYN_SET_GROUP_GAP)
        yield TextArea(
            self._aux_text,
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            id="mef-auxiliary",
            classes="mef-yaml",
        )
        with VerticalScroll(id="mef-kwargs-hint-scroll"):
            yield Static(
                KWARGS_HINT,
                id="mef-kwargs-hint",
                classes=_CLS_ONB_HINT_ONB_DYN,
                markup=False,
            )
        yield Static(
            connection_line_text(None),
            id="mef-probe-line",
            classes="set-hint set-dyn",
        )
        # Строка ошибки монтируется сразу и пустой (display гасит место под неё):
        # remove()+mount() с тем же id в одном такте даёт DuplicateIds, если форма
        # падает на одной ошибке два submit() подряд (урок донора).
        #
        # `markup=False` — единственный Static формы, в который интерполируется НАБРАННОЕ
        # человеком (`_fail`: алиас в отказе). Квадратные скобки Rich читает разметкой:
        # `[x]` съедал имя из отказа, `[/x]` ронял `MarkupError` наружу — и валидатор
        # такие имена пропускает, а файл их квотирует, то есть падало на повторном вводе
        # СОХРАНЁННОГО имени. Тот же приём, каким чинён этот класс в `settings._singleton`.
        error = Static(
            "",
            id="mef-error",
            classes="onb-warn onb-dyn set-group-gap",
            markup=False,
        )
        error.display = False
        yield error

    def on_mount(self) -> None:
        # Адрес — программной записью (`write_field`): обычное присваивание постит
        # `Input.Changed`, и пресет-лист сужался бы по собственному значению поля
        # (живой смок 2026-08-04 «Подключения»). Состояние пары считаем ПОСЛЕ него —
        # оно выводится из поля.
        write_field(self._url_input, self._url0)
        self._sync_pair_state()
        self._focus_position()
        # Строка «Alt+↓ — перечитать список» — с ПЕРВОГО кадра (канон
        # `sync_hint`, смок 2026-08-20): список сетевой (`promise_empty`), и до
        # этого вызова способ позвать его не был назван нигде, пока курсор не
        # заходил на поле id (живой смок 2026-09-04).
        self._list.sync_hint()

    # --- навигация ---
    def _sync_pair_state(self) -> None:
        """Перерисовать всё, что зависит от состояния пары: строку над полями,
        редактируемость токена и активность галки."""
        own = self.own_connection()
        if own == self._pair_own:
            return
        self._pair_own = own
        for w in self.query("#mef-pair"):
            w.update(self._pair_row_text(own))
        # Отказ живёт ровно столько, сколько состояние, о котором он сказан: оба отказа
        # пары («задан свой адрес…», «задан свой токен…») с её сменой перестают быть
        # правдой, и оставленная строка спорила бы со строкой над полями.
        self._clear_error()
        for w in self.query(_SEL_MEF_NOAUTH):
            w.update(self._noauth_text())
        self._sync_token_visibility()

    def _pair_row_text(self, own: bool) -> str:
        """Строка НАД парой: откуда пара взята (§4). У своей два происхождения, и
        читаются они по-разному — знакомые поля формы либо секция `backend`, до которой
        форма не достаёт. У общей — тоже два: раздел «Подключение» либо заслонённая
        запись основного конфига, у которой тёзка берёт пару."""
        if not own:
            return (
                _PAIR_SHARED_BASE
                if self._inherits_from_base()
                else _PAIR_SHARED
            )
        return _PAIR_OWN_BACKEND if self._pair_in_backend() else _PAIR_OWN

    def _sync_token_visibility(self) -> None:
        """● — поле не исчезает (раскладка не прыгает), а замораживается: disabled,
        пустое, placeholder «не используется». ○ — оживает с прежним placeholder.

        Общая пара замораживает его по другой причине (§4): решать про токен нечего, он
        придёт вместе с адресом, — а набранный над общим адресом токен был бы ровно
        запрещённой половиной."""
        inp = self.query_one("#mef-token", Input)
        inp.disabled = self._no_auth or not self.own_connection()
        if self._no_auth:
            inp.value = ""
            inp.placeholder = _NO_AUTH_FROZEN_PLACEHOLDER
        else:
            inp.placeholder = self._token_placeholder()
        # Маркер живёт вместе с placeholder'ом: при ● текст «не используется» — не
        # маска, и поле не должно врать, что сохранённое значение есть.
        inp.set_class(not self._no_auth and self._token_is_mask(), "has-saved")
        # Подпись обещает Ctrl+U по тем же условиям, что и клавиша нажимается: под ●
        # поле заморожено и курсором пропускается — обещание уходит вместе с маской.
        self._sync_token_label()

    def _skip_position(self, position) -> bool:
        """Позиция, на которой курсору делать нечего: замороженное (disabled) поле
        фокусировать нельзя, и стоять на нём — тупик без обратной связи. Заморозок две, и
        обе про токен: галка «без авторизации» и общая пара (§4) — при общем адресе и
        поле, и сама галка неактивны, а почему, сказано в строке галки. Живёт отдельным
        предикатом, а не веткой в `move`: там ей место среди навигации."""
        if not self.own_connection():
            return position == "mef-token" or position is None
        return position == "mef-token" and self._no_auth

    def on_click(self, event) -> None:
        """Клик мышью по полю — тоже перемещение курсора (спека §4.3, канон
        voice-редакторов): без приземления клик в поле id не запускал
        автоподгрузку списка, статуса не было, а `Alt+↓` молчал — его гейт
        смотрит на позицию курсора, которую двигали только стрелки (живой смок
        2026-09-04). Именно КЛИК, не DescendantFocus: события фокуса
        доставляются отложенно, и событие от поля, которое `move` уже покинул,
        перетаскивало курсор назад. Клик по текущему полю — no-op (гейт
        `pos == _idx`: раскрытый список не пересобирается); замороженные
        позиции (`_skip_position`) мышь не оживляет — там и клавиатура не
        стоит. Событие не глушится — выделение текста живёт как жило."""
        if self._interaction_gate is not None and self._interaction_gate():
            # Висит вопрос секции: мышь молчит, как клавиши, — но Textual уже
            # сфокусировал Input на MouseDown, и печать уезжала бы в поле при
            # ЗАМОРОЖЕННОМ составе транзакции («правки под баннером снимку
            # разъехаться не дают» — канон секции; живой смок 2026-09-04).
            # Снимаем фокус обратно: под вопросом клавиатура принадлежит
            # вопросу целиком.
            self.screen.set_focus(None)
            return
        wid = getattr(getattr(event, "control", None), "id", None)
        if wid == "mef-noauth":
            wid = None  # позиция галки в _positions — None (ревью N7, н.3)
        elif wid is None or wid not in self._positions:
            return
        pos = self._positions.index(wid)
        if pos == self._idx or self._skip_position(wid):
            return
        # Тот же хвост, что у move(): чужие списки закрываются перед переездом.
        if self._list.is_open:
            self._list.close()
        if self._presets_ctrl.is_open:
            self._presets_ctrl.close()
        self._idx = pos
        self._focus_position()
        # Футер экрана судит по позиции курсора — мышь обязана обновить его,
        # как канон voice (settings.on_descendant_focus; ревью N7, находка 2).
        getattr(self.screen, "_refresh_footer", lambda: None)()

    def move(self, delta: int) -> None:
        if self._list.move(delta) or self._presets_ctrl.move(delta):
            # Списки модальные: пока по раскрытому есть чем ходить, ↑/↓ водят ПО НЕМУ —
            # курсор формы остаётся на поле до Enter/Esc.
            return
        if self._list.is_open:
            # Фильтр не оставил строк: водить нечем, и держать клавишу себе значит
            # запереть стрелки без обратной связи (контракт `FieldListController.move`).
            self._list.close()
        if self._presets_ctrl.is_open:
            self._presets_ctrl.close()
        self._idx = (self._idx + delta) % len(self._positions)
        while self._skip_position(self._positions[self._idx]):
            self._idx = (self._idx + delta) % len(self._positions)
        self._focus_position()

    def _focus_position(self) -> None:
        """Приземление курсора: подсветка галки, раскрытие/подсказки списков, фокус."""
        target = self._positions[self._idx]
        if target == "mef-url":
            # Канон 2026-08-17: раскрываем, только если поле пусто (решает `can_open`).
            # Есть адрес — список остаётся свёрнутым, ↑/↓ водят по форме, а способ
            # его вызвать назван постоянной строкой под полем (Alt+↓).
            self._presets_ctrl.open()
        if target == "mef-model":
            # Ключ (адрес, авторизация) сверяется ДО развилки «показать добытое /
            # сходить заново» (находка M7 ревью MR #80): список принадлежит СВЯЗИ,
            # а не полю, и после смены адреса или токена добытое у прежнего
            # эндпоинта показывать нельзя — Enter вписал бы id, которого на новом
            # хосте нет. Сверка в одной точке потребления ловит все пути правки
            # разом — печать, пресет, Ctrl+U, галку: ключ выводится из текущего
            # состояния, а не из событий (write_field события не постит).
            key = (self._current_url(), self._current_auth_header())
            if key != self._autoload_key:
                # Автоподгрузка при входе: к этому полю курсор приходит после
                # адреса и токена, идти есть по чему. Те же параметры второй раз
                # не перечитываем (дребезг), ИЗМЕНЁННЫЕ — обязаны: сбой без токена
                # и повтор с введённым токеном это один сценарий, а не два. Сбой
                # не блокирует ручной ввод, Alt+↓ остаётся принудительным
                # перечитыванием.
                self._autoload_key = key
                self._start_models_fetch(refresh=False)
            elif self._models:
                # Список уже подгружен ЭТОЙ связью — показать варианты сразу, но
                # по тому же правилу (`can_open`): над пустым полем сам, над
                # набранным id — только по явному Alt+↓.
                if not self._list.is_open:
                    self._list.open(filter_text=self._model_value())
            elif self._list_status:
                # Этими же параметрами уже сходили — пока курсор был на другом
                # поле, сказать об исходе было негде. Дребезг загрузку не
                # повторит, и молчание читалось бы как «список ещё летит»:
                # говорим то же, что сказали бы, останься курсор здесь.
                self._set_status(self._list_status)
        else:
            # Счётчик/ошибка списка — про поле, с которого курсор ушёл: «2 модели»
            # под чужим полем не про что.
            self._set_status("")
        row = self.query_one(_SEL_MEF_NOAUTH, Static)
        (row.add_class if target is None else row.remove_class)("selected")
        strip_row = self.query_one("#mef-strip", Static)
        (
            strip_row.add_class
            if target == "mef-strip"
            else strip_row.remove_class
        )("selected")
        if target is None or target == "mef-strip":
            # Галка и селектор — не Input: снять фокус явно, иначе прежний
            # фокусный Input «съедал» бы Space и печать.
            self.screen.set_focus(None)
        else:
            self.query_one(f"#{target}", Widget).focus()

    def refocus(self) -> None:
        """Вернуть ввод в поле под курсором — и только это.

        Не `_focus_position`: тот ПРИЗЕМЛЯЕТ курсор на новую позицию (раскрывает
        списки, а над пустым полем модели ещё и идёт за ними в сеть). Здесь курсор
        никуда не двигался — вернулось лишь право поля принимать клавиши."""
        target = self._positions[self._idx]
        if target is None or target == "mef-strip":
            self.screen.set_focus(None)  # галка и селектор: поля ввода нет
        else:
            self.query_one(f"#{target}", Widget).focus()

    def keeps_key(self, key: str) -> bool:
        """Enter и стрелки внутри YAML остаются у TextArea.

        У первой строки ↑ и у последней ↓ уходят форме. Раскрытый список модели
        или пресетов клавишу не держит."""
        target = self._positions[self._idx]
        if target not in ("mef-main", "mef-auxiliary"):
            return False
        if self._list.is_open or self._presets_ctrl.is_open:
            return False
        if key == "enter":
            return True
        area = self.query_one(f"#{target}", TextArea)
        if key == "up":
            return not area.cursor_at_first_line
        if key == "down":
            return not area.cursor_at_last_line
        return False

    def toggle_no_auth(self) -> bool:
        """Space на позиции галки: переключить «без авторизации». True — курсор был
        на галке (переключено); False — клавиша не наша, хост её НЕ стопит (пробел
        печатается в фокусное поле как обычно).

        Гейт двойной — позиция И состояние пары: при общей паре галка неактивна (§4), и
        курсор на неё не встаёт, но решает это навигация. Держать «можно ли переключать»
        на одной навигации значит поручить правило тому, кто его не знает."""
        if self._positions[self._idx] is not None or not self.own_connection():
            return False
        self._no_auth = not self._no_auth
        self.query_one(_SEL_MEF_NOAUTH, Static).update(self._noauth_text())
        self._sync_token_visibility()
        self._sync_pair_state()
        return True

    def cycle_strip_think(self) -> bool:
        """Пробел на строке ``strip_think``: default → true → false → default.

        ``default`` в файл не пишется. False — курсор не здесь, пробел печатается."""
        if self._positions[self._idx] != "mef-strip":
            return False
        nxt = _STRIP_CYCLE.index(self._strip_think) + 1
        self._strip_think = _STRIP_CYCLE[nxt % len(_STRIP_CYCLE)]
        self.query_one("#mef-strip", Static).update(self._strip_text())
        return True

    def _strip_text(self) -> str:
        return f"strip_think: {self._strip_think}"

    @property
    def can_return_shared_pair(self) -> bool:
        """Условие работы Ctrl+U: пара своя И курсор на одном из её полей.

        Предикат живёт на уровне ФОРМЫ, а не поля токена (§5): под галкой ● это поле
        `disabled` и курсором пропускается, и гейт «курсор на токене» отвечал бы «нет»
        ровно там, где клавиша обязана работать. Состояние пары — по совокупности
        (`own_connection`), а не по своему токену: над своим адресом без токена клавиша
        и есть выход из запрещённой половины.

        Позиция всё же нужна: на алиасе и id модели у Ctrl+U своё штатное дело
        (`Input.delete_left_all`), и там оно настоящее — поля предзаполнены.

        Пара во втором написании (секция `backend`) исключена `_pair_returnable`: форма
        её не правит, вернуть наследование отсюда нечем — и «сделал» не должно
        выглядеть так же, как «не сделал».

        Отдельным предикатом, а не веткой внутри действия (эталон — `ConnectionEditor.
        on_token_field_with_saved`): по нему же гейтится обещание в футере, а спросить
        действием нельзя — оно меняет состояние."""
        return (
            self._pair_returnable()
            and self._positions[self._idx] in _PAIR_FIELDS
        )

    def clear_token(self) -> bool:
        """`Ctrl+U` на поле пары: вернуть записи ОБЩЕЕ подключение — снять свой адрес,
        свой токен и галку разом. True — обработали, клавиша наша.

        Пара, а не токен (§5): своё подключение задаётся целиком, и снятая половина
        либо унесла бы общий ключ на чужой хост, либо уехала вслед за сменой общего
        адреса. Имя метода — про клавишу, а не про действие: тем же именем её ведут
        `ConnectionEditor` и хозяин-экран.

        Отличие от `ConnectionEditor.clear_token` не в механике, а в последствии: там
        над токеном слоя нет, и Ctrl+U его СТИРАЕТ (`Authorization: ""`); здесь слой
        есть, и снятие своего не оставляет запись без авторизации — она начинает
        работать общей парой. Поэтому и в файл уедут не пустые строки, а УДАЛЕНИЕ
        ключей `url`/`token` (`submit`).

        Действует сразу: маска своего секрета уходит, placeholder называет маской то,
        что теперь унаследуется, проба с этого момента идёт унаследованной парой —
        результат виден ДО сохранения. Передумать можно, набрав пару заново (пометка
        снимется сама, `on_input_changed`)."""
        if not self.can_return_shared_pair:
            return (
                False  # возвращать нечего либо курсор не там — клавиша не наша
            )
        self._return_shared_pair()
        return True

    def _return_shared_pair(self) -> None:
        """Само действие «вернуть наследование» — у него ДВА входа (§5): клавиша `Ctrl+U` и
        пункт `раздел «Подключение» · наследовать` первым в списке адресов. Разъедься они —
        два пути к одному состоянию оставляли бы разные записи.

        Токен помечается снятым только если в записи он есть: `None` в `submit()` —
        просьба УДАЛИТЬ ключ, и на записи без него это была бы правка из ничего
        (форма показала бы «изменено» там, где менять нечего)."""
        write_field(self.query_one(_SEL_MEF_URL, Input), "")
        write_field(self.query_one("#mef-token", Input), "")
        self._no_auth = False
        self._token_cleared = self._token_current is not None
        self.query_one(_SEL_MEF_NOAUTH, Static).update(self._noauth_text())
        self._sync_token_visibility()
        self._sync_pair_state()
        # Позиция под курсором могла ЗАМЁРЗНУТЬ этим же действием (токен при общей паре):
        # `disabled` у фокусного Input Textual разруливает сам — уводит фокус на соседа по
        # focus chain, то есть на алиас, где Input делает select-all, — а `_idx` формы
        # остаётся на токене. Печать после этого заменяла ИМЯ записи, Enter сохранял
        # переименование (с каскадом по слотам и привязкам), а клавиши позиции (Alt+↓,
        # Space) молчали. Форма — хозяин курсора: заморозила позицию — сама её и покидает,
        # той же дисциплиной, какой `move` обходит `_skip_position`. Цель — «Адрес»: первое
        # поле пары и единственный незамороженный сосед. НЕ через `_focus_position`: он
        # раскрыл бы пресет-лист над опустевшим полем, и следующий Enter подставил бы строку
        # списка вместо сохранения.
        if self._skip_position(self._positions[self._idx]):
            self._idx = self._positions.index("mef-url")
            self.query_one(_SEL_MEF_URL, Input).focus()
        # Связь, по которой ходила проба, изменилась (`_current_url`/`_current_auth_
        # header` с этого момента отдают унаследованные), а `Input.Changed` по
        # программной записи не летит — обесценивать вердикт приходится руками, иначе
        # на экране остаётся «✓» про проверку СВОЕЙ парой.
        self._invalidate_probe()

    # --- списки: пресеты адреса и модели эндпоинта ---
    def _apply_preset(self, preset) -> None:
        """Enter по строке пресета. Пункт-наследование (duck-атрибут `.inherit`) —
        осознанно ОСТАВИТЬ пару пустой: подстановка адреса убила бы живую связь с
        основным конфигом (контракт `preset_list_controller`, `value_of`)."""
        if getattr(preset, "inherit", False):
            # Пункт «наследовать» — «вернуть ОБЩИЕ», а не «очистить адрес» (§5, два
            # входа в ОДНО действие): оставленный токен был бы ровно запрещённой
            # половиной, а поле токена при общей паре заморожено — снять его стало бы
            # нечем. Уходят разом набранное, галка и сохранённый ключ записи.
            self._return_shared_pair()
            return
        write_field(self.query_one(_SEL_MEF_URL, Input), preset.url)
        self._sync_pair_state()
        self._invalidate_probe()

    def _apply_model(self, model_id: str) -> None:
        write_field(self.query_one("#mef-model", Input), model_id)
        # write_field глушит Input.Changed (иначе список сужался бы сам по себе) —
        # призрак алиаса пересчитываем руками, ему это событие и было нужно.
        self._refresh_alias_placeholder()
        self._invalidate_probe()

    def reload_presets(self) -> bool:
        """`Alt+↓` на поле адреса: раскрыть свёрнутый пресет-лист (канон локального
        источника — «показать список»). Над заданным адресом список сам не
        раскрывается (канон 2026-08-17, `can_open`), а печать без совпадений его
        сворачивает (канон в редакции 2026-08-18) — эта клавиша и есть единственный
        путь назад, который постоянная строка под полем обещает.

        `False` — курсор не на адресе либо список уже раскрыт: перечитывать у
        статического источника нечего."""
        if self._positions[self._idx] != "mef-url":
            return False
        return self._presets_ctrl.reload()

    def reload_models(self) -> bool:
        """`Alt+↓` на поле id: сходить на эндпоинт за списком моделей. Это ПОДБОР, не
        проба (роли клавиш разведены, контракт п.4).

        Сетевой источник строк: список идёт в сеть и раскрытым, и свёрнутым, поэтому
        клавиша всегда значит «перечитать» (`MODEL_RELOAD_HINT`) — раскрытие само по
        себе здесь ничего не значило бы. `False` — курсор не на поле id, клавиша не
        наша."""
        if self._positions[self._idx] != "mef-model":
            return False
        # Ручное перечитывание = автоподгрузка этими параметрами уже состоялась:
        # иначе уход с поля и возврат сходили бы в сеть ещё раз тем же ключом.
        self._autoload_key = (self._current_url(), self._current_auth_header())
        return self._start_models_fetch(refresh=True)

    def _start_models_fetch(self, *, refresh: bool) -> bool:
        """Уйти за списком моделей. `refresh=False` — автоподгрузка при входе в поле
        (кэш годится), `True` — принудительное перечитывание по Alt+↓."""
        url = self._current_url()
        if not url:
            self._set_status(_LIST_NO_URL)
        else:
            env_missing = _env_missing_line(url, self._current_token_raw())
            if env_missing:
                self._set_status(env_missing)
            else:
                auth = self._current_auth_header()
                # Поколение бампаем ЗДЕСЬ: `exclusive=True` уже бегущий поток не
                # останавливает, и ответ прошлой загрузки перемонтировал бы список поверх
                # свежего (идиома `ModelEditor._start_fetch`).
                self._list_gen += 1
                gen = self._list_gen
                self._set_status(LIST_LOADING)
                # `group` задан явно: `exclusive` отменяет соседей ПО ГРУППЕ, а не по `name`,
                # и в общей группе «default» проба гасила бы летящий подбор и наоборот —
                # клавиши разведены по ролям, значит и по воркерам.
                self.run_worker(
                    lambda: self._fetch_blocking(url, auth, gen, refresh),
                    thread=True,
                    exclusive=True,
                    group="model-entry-list",
                    name="model-entry-list-fetch",
                )
        return True

    def _fetch_blocking(
        self, url: str, auth: str | None, gen: int, refresh: bool
    ) -> None:
        url, auth = _resolved_for_request(url, auth)
        models = _fetch_models_trying_auth(
            self._fetch,
            url,
            auth,
            verify=self._verify,
            config_proxy=self._config_proxy,
            refresh=refresh,
        )
        self.app.call_from_thread(self._on_models, models, gen, refresh)

    def _on_models(self, models, gen: int, refresh: bool) -> None:
        # Поколение протухло — ответ не про эту загрузку: не трогаем ни состояние,
        # ни DOM.
        if gen != self._list_gen or not self.is_mounted:
            return
        if self._positions[self._idx] != "mef-model":
            # Курсор успел уйти с поля: список запоминаем (вернётся — раскроется
            # сам), но раскрывать и вешать счётчик под чужим полем нельзя. Провал
            # запоминаем тем же движением: забыть его значит встретить вернувшийся
            # курсор пустым полем без объяснения — дребезг автоподгрузки повторить
            # загрузку теми же параметрами не даст.
            self._models = list(models or [])
            self._list_status = _list_outcome(models)
            return
        if not models:
            # Исход вместо молчания: сервер мог лежать, а клавиша выглядела бы
            # мёртвой. Вписать id руками при этом никто не мешает.
            self._models = []
            self._list_status = _list_outcome(models)
            self._list.close()
            self._set_status(self._list_status)
            return
        self._models = list(models)
        self._list_status = None
        self._list.close()  # прошлое раскрытие больше не наше
        # `forced` — только у явной клавиши (`Alt+↓` → `refresh=True`): автоподгрузка
        # обязана спрашивать СОБСТВЕННЫЙ гейт формы (`can_open`) — форсированный
        # ответ раскрывался поверх набранного id, забирал ↑/↓ списку, и один Enter
        # молча подменял `gpt-4` на `gpt-4-turbo` (находка M6 ревью MR #80).
        self._list.open(forced=refresh, filter_text=self._model_value())
        n = len(self._models)
        self._set_status(f"{n} {plural_models(n)}")

    def _set_status(self, text: str) -> None:
        """Строка состояния списка ПОД рамкой (§3.2.2: внутри рамки только выбор).
        Пустой текст = строки нет: `display` гасит и место под неё."""
        for w in self.query("#mef-model-status"):
            w.update(text)
            w.display = bool(text)

    def enter(self) -> bool:
        """Enter по раскрытому списку — подставить выделенное (True, клавиша наша);
        списков нет — False, и хозяин трактует Enter как «сохранить» (submit)."""
        if self._presets_ctrl.is_open:
            self._presets_ctrl.pick()  # подстановку делает `_apply_preset`
            return True
        if self._list.is_open:
            self._list.pick()
            return True
        return False

    def escape(self) -> bool:
        """Esc сворачивает СВОЁ под-состояние (раскрытый список) и съедает клавишу;
        сворачивать нечего — False, и Esc достаётся хозяину (тот закроет форму, а
        черновик умрёт вместе с ней). Порядок вложенности из Global Constraints."""
        if self._presets_ctrl.is_open:
            self._presets_ctrl.close()
            return True
        if self._list.is_open:
            self._list.close()
            self._set_status("")  # счётчик свёрнутого списка ни о чём
            return True
        return False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mef-model":
            value = event.input.value
            if self._list.is_open:
                self._list.apply_filter(value)
            elif self._models:
                # Печать возвращает свёрнутый список с фильтром — как у адреса
                # и голосового endpoint'а (`reopen_for_typing`: Esc вернёт
                # исходный id, не набранное). Поле id было единственным соседом
                # без живого фильтра (живой смок 2026-09-04). Без добытых
                # моделей раскрывать нечего — Alt+вниз остаётся походом в сеть.
                self._list.reopen_for_typing(value)
            self._refresh_alias_placeholder()
        elif event.input.id == "mef-url":
            # Канон 2026-08-17 в редакции 2026-08-18 (эталон — `ConnectionEditor`):
            # список раскрыт, пока значение складывается И есть из чего выбирать.
            # Оба множителя проверяются здесь и только по текущему тексту, а не по
            # предыстории поля, — иначе с пустого поля печать фильтровала бы, а с
            # заполненного не делала ничего.
            value = event.input.value
            if not self._presets_ctrl.would_match(value):
                # Набранного в каталоге нет: список сворачиваем (предлагать нечего),
                # но не молчим — строка под полем говорит, что совпадений не нашлось,
                # и чем список вернуть. Раскрытым пустым он обещал бы «Enter —
                # оставить свой адрес» там, где Enter сохраняет форму.
                self._presets_ctrl.close(closed_hint=PRESET_NO_MATCH_CLOSED)
            elif self._presets_ctrl.is_open:
                self._presets_ctrl.apply_filter(value)
            else:
                # `reopen_for_typing`, а не `open()`: он не трогает `_value0`, и Esc
                # после раскрытия по печати вернёт исходный адрес, а не набранное.
                self._presets_ctrl.reopen_for_typing(value)
                self._presets_ctrl.apply_filter(value)
        elif (
            event.input.id == "mef-token"
            and event.input.value
            and self._token_cleared
        ):
            # Сняли свой токен, а потом набрали новый — пометка снимается сама.
            # Иначе submit пошёл бы по ветке «удалить ключ» и набранное молча
            # пропало бы. Подпись возвращается вместе с пометкой: Ctrl+U снова
            # работает, и молчать о нём нельзя.
            self._token_cleared = False
            self._sync_token_label()
        if event.input.id in ("mef-url", "mef-token"):
            # Печать в паре меняет её состояние: адрес — вход в «своё» и выход из него,
            # набранный токен — вторая половина. Пересчитываем здесь же, как призрак
            # алиаса в ветке поля id: своя правка — свой пересчёт.
            self._sync_pair_state()
        if event.input.id in ("mef-url", "mef-token", "mef-model"):
            self._invalidate_probe()

    def _invalidate_probe(self) -> None:
        """Значения, по которым ходила проба, изменились — прежний вердикт больше не
        про эту связь. Молчание честнее залипшего ✓; текст «не проверено» — тот же
        общий рендер, что и все прочие вердикты (`connection_line_text(None)`)."""
        self._probe_gen += 1  # летящая проба тоже устарела
        self._set_probe_line(connection_line_text(None))

    # --- проба (Ctrl+Space) ---
    def _current_url(self) -> str:
        """Адрес пробы и списка: поле, иначе унаследованный (`_inherited_url`: у тёзки
        — адрес заслонённой записи, иначе эффективный из основного конфига) — то, по
        чему запись реально пойдёт.

        Пара своя, а адрес не набран — собранной пары нет вовсе: общий адрес такой
        записи не достанется (её просто не сохранить), и проба на него отвечала бы не
        про то. Молчим тем же «сначала укажите адрес»."""
        own_url = self._url_value()
        if own_url:
            return own_url
        return "" if self.own_connection() else self._inherited_url()

    def _current_auth_header(self) -> str | None:
        """Authorization тем же правилом, каким будет жить запись: ● — честно без
        токена; введённый текст; собственный токен записи; иначе унаследованный
        (`_inherited_auth`). Поле и проба обязаны говорить об одной и той же связи.

        В сеть уходит только СОБРАННАЯ пара (§4): унаследованный токен принадлежит
        унаследованному адресу и уходит только вместе с ним, а свой — своему. Иначе
        «напечатал чужой адрес → перешёл на поле id» отправлял бы общий ключ на чужой
        хост автоподгрузкой списка, ещё до всякого сохранения."""
        if self._no_auth:
            return None
        if not self.own_connection():
            return self._inherited_auth() or None
        token_raw = self._field_value("mef-token")
        if token_raw:
            return onboarding.authorization_header(token_raw)
        if self._own_token_active():
            return onboarding.authorization_header(self._token_current)
        return None

    def _current_token_raw(self) -> str | None:
        """СЫРОЙ токен, с которым уйдёт запрос, — тем же правилом, каким собирает
        заголовок `_current_auth_header`: набранное в поле, иначе действующий свой
        токен записи. Нужен гейту `${ENV:…}`: собранный заголовок называл бы
        переменную вперемешку со схемой («Bearer ${ENV:X} · переменной нет»), а в
        файле лежит и не подставляется именно ссылка.

        УНАСЛЕДОВАННЫЙ токен сюда не попадает, и это не забывчивость: `effective_auth`
        приезжает из УЖЕ РЕЗОЛВНУТОГО конфига (`settings._probe_args_from_config`
        читает `self._config`, а не файл), тень — из резолвнутого снимка
        (`settings._base_registry_snapshot`) — ссылки в них физически нет, называть
        нечего, и выдуманное имя переменной было бы хуже молчания. Гейт наследованной
        половины возможен только вместе с сырым значением, приехавшим сюда с ней."""
        if self._no_auth or not self.own_connection():
            return None
        return self._token_value() or (
            self._token_current if self._own_token_active() else None
        )

    def probe_connection(self) -> bool:
        """`Ctrl+Space`: проверить связь по тем значениям, что стоят в полях СЕЙЧАС.
        Это ПРОБА, не подбор списка (контракт п.4).

        Вердикт — ТОТ ЖЕ дифференциальный, что в «Подключении», и тем же общим
        `settings_probe.probe_connection`: контрольный запрос БЕЗ токена отвечает,
        требует ли сервер авторизацию вообще; только на 401 идёт повтор С токеном;
        спорный отказ (не 401 и не 2xx) добирается КАНАРЕЙКОЙ — заведомо негодным
        токеном, и совпавший с ним ответ значит, что сервер токены не различает и
        «принят» заявлять нельзя. Своя копия этого танца здесь и жила до синка —
        без канарейки, то есть врала ровно там, где канон 2026-08-17 научился
        молчать. Текст вердикта — общий `connection_line_text`."""
        url = self._current_url()
        if not url:
            self._set_probe_line(Text(_PROBE_NO_URL, style=DEEP_DIM))
        else:
            env_missing = _env_missing_line(url, self._current_token_raw())
            if env_missing:
                self._set_probe_line(Text(env_missing, style=WARN))
            else:
                auth = self._current_auth_header()
                model = self._model_value()
                self._probe_gen += 1
                gen = self._probe_gen
                self._set_probe_line(Text(_PROBE_RUNNING, style=DEEP_DIM))
                self.run_worker(
                    lambda: self._probe_blocking(url, auth, model, gen),
                    thread=True,
                    exclusive=True,
                    group="model-entry-probe",
                    name="model-entry-probe",
                )
        return True

    def _probe_blocking(
        self, url: str, auth: str | None, model: str, gen: int
    ) -> None:
        from gc_tui.services.settings_probe import (
            ProbeResult,
            probe_connection,
        )

        url, auth = _resolved_for_request(url, auth)
        try:
            result = probe_connection(
                url,
                auth,
                self._verify,
                model,
                config_proxy=self._config_proxy,
                probe_fn=self._probe,
            )
        except Exception as exc:
            result = ProbeResult(
                "unknown", None, None, 0, f"внутренняя ошибка проверки: {exc}"
            )
        self.app.call_from_thread(self._on_probe, result, gen)

    def _on_probe(self, result, gen: int) -> None:
        if gen != self._probe_gen or not self.is_mounted:
            return
        self._set_probe_line(connection_line_text(result))

    def _set_probe_line(self, text: Text) -> None:
        for w in self.query("#mef-probe-line"):
            w.update(text)

    # --- контракт для хозяина ---
    # Признаки состояния — read-only и БЕЗ сайд-эффектов: футер секции позиционный
    # (Enter значит разное на разных строках), а обещания клавиш зависят ещё и от
    # того, раскрыт ли список и есть ли в нём подсвеченная строка. Спросить это
    # действием нельзя — `reload_models()` уходит в сеть. Зеркало `ConnectionEditor`
    # (`on_url_field`/`presets_open`/`presets_have_selection`).
    @property
    def on_url_field(self) -> bool:
        """Курсор на адресе: только здесь работает `Alt+↓ — показать список`."""
        return self._positions[self._idx] == "mef-url"

    @property
    def on_model_field(self) -> bool:
        """Курсор на поле id: только здесь работает `Alt+↓ — перечитать список`."""
        return self._positions[self._idx] == "mef-model"

    @property
    def on_no_auth_row(self) -> bool:
        """Курсор на строке-галке: Space переключает её, и только на ней."""
        return self._positions[self._idx] is None

    @property
    def on_strip_row(self) -> bool:
        return self._positions[self._idx] == "mef-strip"

    @property
    def presets_open(self) -> bool:
        return self._presets_ctrl.is_open

    @property
    def list_open(self) -> bool:
        return self._list.is_open

    @property
    def presets_have_selection(self) -> bool:
        """В раскрытом списке есть подсвеченная строка — Enter подставит ИМЕННО её.
        Не то же, что «список не пуст»: форсированное раскрытие поверх своего адреса
        показывает все строки, не подсвечивая ни одной, — Enter там оставит своё."""
        return self._presets_ctrl.has_selection

    @property
    def list_has_selection(self) -> bool:
        return self._list.has_selection

    def _field_value(self, field_id: str) -> str:
        return self.query_one(f"#{field_id}", Input).value.strip()

    def _yaml_text(self, field_id: str) -> str:
        return self.query_one(f"#{field_id}", TextArea).text.rstrip()

    def _model_value(self) -> str:
        try:
            return self._field_value("mef-model")
        except NoMatches:
            return self._model0

    def _token_value(self) -> str:
        try:
            return self._field_value("mef-token")
        except NoMatches:
            return ""

    def _url_value(self) -> str:
        """Адрес, набранный в записи. До монтирования поля его знает только `initial` —
        а состояние пары спрашивают и оттуда (`compose`)."""
        try:
            return self._field_value("mef-url")
        except NoMatches:
            return self._url0

    @property
    def original_alias(self) -> str | None:
        """Алиас, под которым запись живёт в файле СЕЙЧАС (None — создание).
        Секция сравнивает его с алиасом из `submit()`: разошлись — переименование,
        транзакция с переездом ссылок."""
        return self._own_alias

    def peek(self) -> dict:
        """Сырые значения полей без сайд-эффектов — для dirty-проверки и хозяина."""
        return {
            "alias": self._field_value("mef-alias"),
            "url": self._field_value("mef-url"),
            "token": self._field_value("mef-token"),
            "model": self._field_value("mef-model"),
            "main": self._yaml_text("mef-main"),
            "auxiliary": self._yaml_text("mef-auxiliary"),
            "strip_think": self._strip_think,
        }

    def is_dirty(self) -> bool:
        try:
            values = self.peek()
        except NoMatches:
            return False
        if values != self._initial_snapshot:
            return True
        if self._token_cleared:
            return True  # Ctrl+U: свой токен помечен к снятию
        # Галка отличается от действующего варианта: «явно без авторизации» — такая
        # же правка, как текст в поле, хотя все поля стоят как были.
        return self._no_auth != (self._token_current == "")

    def _fail(self, message: str) -> tuple[bool, None, None]:
        for w in self.query("#mef-error"):
            w.update(message)
            w.display = True
        return False, None, None

    def _clear_error(self) -> None:
        for w in self.query("#mef-error"):
            w.update("")
            w.display = False

    def submit(self) -> tuple[bool, str | None, dict | None]:
        """(ok, alias|None, fields|None). `fields` — только те ключи, о которых форма
        что-то говорит; merge со старой записью делает писатель.

        Правило пустого поля читается по тому, БЫЛО ли в нём что-то при входе:

        * поле не заполняли (создание; в правке — токен, который не префиллится) —
          ключа нет, старое значение записи остаётся;
        * ПРЕДЗАПОЛНЕННОЕ поле стёрли — ключ `None`, то есть просьба удалить его
          (контракт писателя): запись возвращается к наследованию основного конфига.
          Промолчать здесь было бы враньём — форма показала бы «изменено», Enter
          ответил бы «сохранено», а в файле остался бы прежний адрес.

        Токен предзаполнить нельзя (секрет), поэтому пустое поле здесь всегда значит
        «не трогаем», а два его снятия разведены по СМЫСЛУ и по клавише: галка ●
        «без авторизации» — явное `""` (осознанно без токена СВОЕГО адреса, пара при
        этом остаётся своей и целой), `Ctrl+U` — `None` у обоих ключей пары, то есть
        возврат к общему подключению «Подключения». Одним переключателем это не
        выражается: `""` и «ключа нет» — разные состояния файла, и путь к каждому
        обязан быть свой.

        Ошибка остаётся В ФОРМЕ: сообщение под полями, `ok=False`, писать нечего."""
        values = self.peek()
        alias = values["alias"]
        if not alias:
            # Пустой алиас — имя из id (ровно то, что показывал призрак-placeholder):
            # одинаково при создании и правке — стереть алиас существующей записи
            # значит «переименуй в имя по умолчанию». Выводить не из чего только
            # когда пусто И поле id — честная ошибка.
            if not values["model"]:
                return self._fail(_ALIAS_OR_MODEL_EMPTY)
            alias = self._derived_alias(values["model"])
        try:
            validate_model_alias(alias)
        except ValueError as exc:
            return self._fail(str(exc))
        if alias in self._other_aliases:
            return self._fail(f"алиас '{alias}' уже используется")
        if alias in self._base_aliases and alias != self._own_alias:
            # Имя записи основного конфига: своей такой в профиле нет, дублем это не
            # является — но две строки реестра под одним алиасом читались бы как
            # одна, и отказ говорит, чем имя занято (§5).
            #
            # Кроме СОБСТВЕННОГО имени записи — по тому же правилу, каким запись не
            # считается дублем самой себя (`_other_aliases`). Тёзка базовой в профиле
            # легальна (§4/§5: она её перекрывает, витрина зовёт её своей и обещает
            # «Enter — изменить»), и запрет владельца — про СОЗДАНИЕ такой записи, а не
            # про правку существующей: иначе её нельзя тронуть вовсе, а переименование
            # в выход не годится — оно снимает перекрытие. Переименование В чужое базовое
            # имя это условие по-прежнему отвергает.
            return self._fail(_ALIAS_TAKEN_BY_BASE)
        url = values["url"]
        if (
            url
            and not onboarding._valid_endpoint_url(url)
            and not onboarding.env_ref_status(url)
        ):
            # Ссылка `${ENV:VAR}` — штатное СЫРОЕ значение поля: форма читает документ
            # без резолва (`models_section._read_document`), а `docs/configuration.md`
            # сам зовёт хранить адрес и токен так. Структурная проверка её бракует, и
            # запись с таким адресом нельзя было сохранить даже правкой одного id модели
            # — тупик с выходом только через ручную правку YAML. Гейт по ЗНАЧЕНИЮ, а не
            # по «трогали ли поле»: иначе плейсхолдер нельзя было бы и набрать. То же
            # исключение, что у пробы мастера (`screens/onboarding`).
            return self._fail(_URL_ERROR)
        # §2: своё подключение задаётся ЦЕЛИКОМ — половину пары форма не сохраняет.
        # Иначе отказ пришёл бы от валидатора конфига, когда правка уже сделана.
        has_address, has_token = self._pair_parts()
        if has_address != has_token:
            names = self._pair_env_missing()
            if names:
                # Половину пары съела НЕПОДСТАВЛЕННАЯ ссылка, а не ошибка в полях:
                # отправить человека искать опечатку там, где он забыл `export`, —
                # худшее, что тут можно сказать. Тот же разбор делает валидатор
                # загрузки (`model_registry.validate_registry_pairs`).
                return self._fail(
                    _PAIR_ENV_MISSING.format(names=", ".join(names))
                )
            return self._fail(
                _PAIR_NEEDS_TOKEN if has_address else _PAIR_NEEDS_URL
            )
        try:
            main = parse_completion_kwargs_text(values["main"])
        except ValueError as exc:
            return self._fail(f"main: {exc}")
        try:
            auxiliary = parse_completion_kwargs_text(values["auxiliary"])
        except ValueError as exc:
            return self._fail(f"auxiliary: {exc}")
        self._clear_error()
        self._write_yaml("mef-main", main)
        self._write_yaml("mef-auxiliary", auxiliary)
        fields: dict = {}
        if values["model"]:
            fields["model"] = values["model"]
        elif self._model0:
            # Стёрли предзаполненный id: ключ снят — провайдерским id снова служит сам
            # алиас (`model_router`: `entry.get("model") or alias`).
            fields["model"] = None
        if url:
            fields["url"] = url
        elif self._url0:
            fields["url"] = (
                None  # стёрли свой адрес — вернулись к наследованию
            )
        if self._no_auth:
            if (
                self._token_current != ""
            ):  # не «оставить прежнее ""», а изменение
                fields["token"] = ""
        elif values["token"]:
            fields["token"] = values["token"]
        elif self._token_cleared:
            # Ctrl+U: свой токен снят — ключ УДАЛЯЕТСЯ (`None` — контракт писателя),
            # и запись возвращается к общему токену «Подключения». Именно удаление, а
            # не `token: ""`: пустая строка значит «осознанно без авторизации» (галка
            # ●), и написать её здесь значило бы подменить один смысл другим.
            fields["token"] = None
        elif self._token_current == "":
            # Галку сняли, своего токена не ввели: запись возвращается к наследованию —
            # ключ УДАЛЯЕТСЯ. Промолчать здесь значило бы оставить `token: ""` в файле
            # (пустое поле = «не трогаем») — переключатель работал бы в одну сторону.
            fields["token"] = None
        fields["main"] = main
        fields["auxiliary"] = auxiliary
        # default — None: писатель снимает ключ, и модель снова берёт глобальный флаг.
        fields["strip_think"] = (
            None
            if self._strip_think == "default"
            else self._strip_think == "true"
        )
        return True, alias, fields

    def _write_yaml(self, field_id: str, value: dict | None) -> None:
        """Поле после разбора — раскрытый YAML. ``None`` очищает, ``{}`` остаётся ``{}``."""
        self.query_one(f"#{field_id}", TextArea).load_text(
            _completion_field_text(value)
        )
