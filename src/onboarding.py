"""Онбординг первого запуска: политика «настроен ли конфиг» + текст-гайд.

Модуль-владелец презентации/политики первого запуска. Контроллеры (cli, tui)
зовут is_unconfigured()/setup_guide(); сюда же вынесены сентинелы дефолт-шаблона
(единый источник истины, сверяется тестом с config.default.yml).

Требования стратегия-зависимы: auth удовлетворяется токеном ИЛИ блоком agent_auth
(сертификатная авторизация — без статического токена). Подсказка перечисляет только
реально незаполненное; если всё покрыто — позитивное «настроен».
"""
from typing import Any, Callable, Dict, List, NamedTuple, Optional
from urllib.parse import urlparse

import config_raw


def authorization_header(raw: str) -> str:
    """Ensure raw token has Authorization scheme; add Bearer if missing.

    If raw contains no space after strip, prepend 'Bearer '.
    If raw has space, keep as-is (Basic, Bearer, or custom scheme).
    """
    s = raw.strip()
    return s if " " in s else f"Bearer {s}"


def mask_authorization(header: str) -> str:
    """Mask authorization header for logging: show scheme and first 3 chars of secret.

    Format: '<scheme> <first 3 chars>***'
    If no scheme+secret pair, return '***'.
    """
    parts = header.split(" ", 1)
    if len(parts) == 2 and parts[1]:
        scheme, secret = parts
        return f"{scheme} {secret[:3]}***"
    return "***"


def mask_secret(raw: str) -> str:
    """Mask a bare secret (a key with no scheme: `gsk_…`, `sk-…`): first 4 chars + … + last 4.

    mask_authorization masks a HEADER — it shows the scheme, and for a bare key
    (no space) degrades to '***', which no longer identifies anything. Here the
    recognisable edges stay visible instead. Short secrets are not cut at all:
    what remains would exceed what is hidden.

    Threshold is 16, not 10: the visible edges take 8 characters, so anything
    shorter shows more than it hides — a 10-character key rendered 8 of its 10
    on screen, exactly the case this rule exists to exclude.
    """
    s = str(raw)
    return f"{s[:4]}…{s[-4:]}" if len(s) >= 16 else "***"


ENV_REF_RESOLVED = "переменная подставилась"
ENV_REF_MISSING = "переменной нет"


def env_ref_status(raw: Any) -> str:
    """Статус ссылки ${ENV:VAR} в СЫРОМ значении поля ("" — значение не ссылка)."""
    if not config_raw.env_refs(raw):
        return ""
    return ENV_REF_MISSING if config_raw.missing_env_refs(raw) else ENV_REF_RESOLVED


def display_value(raw: Any) -> str:
    """Сырое значение поля для показа: ссылка — ссылкой + её статус, литерал — как есть."""
    status = env_ref_status(raw)
    return f"{raw} · {status}" if status else (raw or "")


def display_secret(raw: Any) -> str:
    """То же для секрета: литерал — под маской. Ссылка секретом не является и
    показывается как есть: маска выдавала бы «токен задан» там, где его нет.

    Маску выбираем по форме значения: заголовок «схема секрет» прячем
    mask_authorization (схему показать осмысленно — Bearer/Basic), голый ключ —
    mask_secret (у него схемы нет, и заголовочная маска вырождалась в «***»)."""
    if env_ref_status(raw):
        return display_value(raw)
    return mask_authorization(raw) if " " in str(raw) else mask_secret(raw)


def _valid_endpoint_url(raw: str) -> bool:
    """Структурная проверка адреса: схема http(s) + непустой хост.
    Путь и достижимость НЕ проверяем (осознанно — обрезанный-но-валидный путь пройдёт)."""
    parsed = urlparse(raw)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

# Сентинелы из config.default.yml (страж совпадения — tests/test_onboarding.py).
URL_SENTINEL = "http://your-backend:PORT/v1/chat/completions"
TOKEN_SENTINEL = "Bearer <ВПИШИТЕ_ТОКЕН>"
MODEL_SENTINEL = "<ВПИШИТЕ_МОДЕЛЬ>"


def configured_endpoint(raw: Any) -> str:
    """Адрес, ЗАДАННЫЙ человеком, либо пусто: заглушка шаблона — «ещё ничего не
    задано», а не значение.

    Одно правило на всех, кто адрес показывает, И на всех, кто по нему стучится.
    Витрины его знали, сетевой путь — нет: запрос за списком моделей уходил на
    несуществующий `your-backend:PORT`, и мастер печатал «не удалось получить
    список» человеку, у которого в поле виден его рабочий адрес."""
    return "" if not raw or raw == URL_SENTINEL else str(raw)


def _entry_carries_own_connection(entry: Dict[str, Any]) -> bool:
    """Запись ЗАГРУЖЕННОГО реестра несёт своё подключение целиком.

    Что считать адресом и токеном, спрашиваем у ядра (`connection_parts`, §2) — тем
    же определением судят Router и правило атомарной пары. Своё прочтение ключей
    ошибалось бы в обе стороны: `url: ""` — это «своего адреса нет, пара
    наследуется», а не заданный адрес; своё подключение вдобавок пишется вторым
    равноправным написанием (`backend.completions_url` / `backend.proxy` /
    `backend.chat_completion_headers.Authorization`), и заполненный по документации
    реестр объявлялся бы ненастроенным.

    Форма здесь — снимок (`config._serialize_model_entry`): сырого `token` в
    загруженном конфиге уже нет, вместо него вычисленный `authorization_header`
    (None — наследовать, "" — ЗАДАННЫЙ токен «сервер не требует авторизации»), а
    явные оверлеи backend лежат в `backend_overrides`.

    Адрес и токен спрашиваются вместе, одним ответом: правило пары (§3) не пускает
    в загруженный конфиг половину подключения, поэтому «свой адрес» и «свой токен»
    у записи — одно и то же условие."""
    from config_modules.model_registry import connection_parts

    entry = entry or {}
    overrides = entry.get("backend_overrides") or {}
    headers = overrides.get("chat_completion_headers") or {}
    return all(connection_parts(
        url=entry.get("url"),
        token=entry.get("authorization_header"),
        completions_url=overrides.get("completions_url"),
        proxy=overrides.get("proxy"),
        authorization=headers.get("Authorization"),
    ))


def _registry_is_self_sufficient(c: Dict[str, Any]) -> bool:
    """КАЖДАЯ запись реестра работает через СВОЁ подключение — тогда плоский
    `backend` не нужен вовсе (спека §4: незаданное у записи наследуется от него).

    Только для `chat_completions`: в `responses` от записи действует один
    провайдерский id, per-model адрес и авторизация мертвы (§4) — там такой конфиг
    ушёл бы на плоский плейсхолдер. Проверка по ВСЕМ записям, а не по резолвнутой,
    намеренно грубее: резолв (слоты, привязки, фоллбэки уровней) — знание Router'а,
    тащить его в детект первого запуска нельзя, а «самодостаточны все» ошибается
    только в безопасную сторону — лишний раз предложить мастер, но не объявить
    готовым конфиг, который уйдёт на `your-backend:PORT`."""
    models = c.get("models") or {}
    return (c.get("backend_api_mode") == "chat_completions"
            and bool(models)
            and all(_entry_carries_own_connection(entry) for entry in models.values()))


def _endpoint_set(c: Dict[str, Any]) -> bool:
    if c.get("completions_url") not in (None, URL_SENTINEL):
        return True
    return _registry_is_self_sufficient(c)


def _legacy_model_set(c: Dict[str, Any]) -> bool:
    """Плоский `agent.model` заполнен (не плейсхолдер шаблона)."""
    return c.get("model") not in (None, "", MODEL_SENTINEL)


def _model_set(c: Dict[str, Any]) -> bool:
    """Требование модели: непустой merged-реестр (любой слой, спека §11) ИЛИ
    заполненный легаси-ключ.

    Сам факт непустого реестра закрывает ТОЛЬКО это требование: записи, которые
    пишет мастер, наследуют url и авторизацию от плоского `backend` (решение 10),
    поэтому такой реестр поверх плейсхолдерного endpoint'а — «нечем работать», а не
    «настроено». Адрес и токен реестр закрывает отдельно и по другому условию —
    когда их несут сами записи (`_registry_is_self_sufficient`)."""
    return bool(c.get("models")) or _legacy_model_set(c)


def _agent_auth_set(c: Dict[str, Any]) -> bool:
    auth_cfg = c.get("agent_auth") or {}
    return bool(auth_cfg) and bool(auth_cfg.get("auth_endpoint"))


def _auth_set(c: Dict[str, Any]) -> bool:
    # Auth «не задан» (pending) ТОЛЬКО если в конфиге остался шаблонный плейсхолдер
    # токена (и нет полноценного agent_auth). Реальный токен ИЛИ намеренное отсутствие
    # заголовка (локальный прокси / эндпоинт без auth сам ставит авторизацию) → задан.
    # Гейт — детектор незаполненного шаблона, а не валидатор auth: отсутствие ≠ сентинел.
    # Симметрично адресу: авторизацию может нести и сама запись реестра — целой парой
    # со своим адресом, порознь их правило пары (§3) в конфиг не пускает.
    auth = (c.get("chat_completion_headers") or {}).get("Authorization")
    if auth == TOKEN_SENTINEL:
        return _agent_auth_set(c) or _registry_is_self_sufficient(c)
    return True


class _Requirement(NamedTuple):
    is_satisfied: Callable[[Dict[str, Any]], bool]
    hint: str   # что вписать в config.yml, если требование не удовлетворено


# Требования к рабочему конфигу. auth — «либо/либо»: токен ИЛИ agent_auth.
# Чтобы добавить требование: дописать строку (+ при необходимости сентинел и
# плейсхолдер в config.default.yml — тест-страж поймает рассинхрон).
REQUIREMENTS: List[_Requirement] = [
    _Requirement(
        _endpoint_set,
        "backend.completions_url — endpoint OpenAI-совместимого провайдера (chat/completions)",
    ),
    _Requirement(
        _auth_set,
        "backend.chat_completion_headers.Authorization — токен доступа (Bearer …)",
    ),
    _Requirement(
        _model_set,
        "agent.model — имя модели",
    ),
]


# Соответствие ключ-якорь (leaf в config.yml) → предикат «поле задано».
# Переиспользует те же предикаты, что и REQUIREMENTS — единый источник политики.
_FIELD_CHECKS: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "completions_url": _endpoint_set,
    "Authorization": _auth_set,
    "model": _model_set,
}

# Dotted-пути якорей полей визарда в config.yml (для path-aware записи и вставки).
FIELD_ANCHOR_PATHS: Dict[str, str] = {
    "completions_url": "backend.completions_url",
    "Authorization": "backend.chat_completion_headers.Authorization",
    "model": "agent.model",
    "proxy": "backend.proxy",
}


def pending_field_keys(config: Dict[str, Any]) -> set:
    """Ключи-якоря полей, ещё не удовлетворённых конфигом (для визарда)."""
    return {key for key, is_set in _FIELD_CHECKS.items() if not is_set(config)}


def pending_requirements(config: Dict[str, Any]) -> List[_Requirement]:
    """Требования, ещё не удовлетворённые текущим конфигом."""
    return [r for r in REQUIREMENTS if not r.is_satisfied(config)]


def is_unconfigured(config: Dict[str, Any]) -> bool:
    """True, если конфиг ещё не готов к запуску (есть незакрытое требование).

    Требования проверяются ВСЕ — реестр моделей входит в них через `_model_set`
    (спека §11: «нечем работать» ловится в обоих мирах), а не закорачивает детект
    целиком: иначе конфиг с живым реестром и плейсхолдерным адресом считался бы
    настроенным, мастер отчитывался бы «✓ Готово», следующий старт больше не
    предложил бы настройку, а запрос ушёл бы на `your-backend:PORT`."""
    return bool(pending_requirements(config))


def _registry_overridden_above(config_path, *, ignore_profile: bool = False) -> bool:
    """Действующий реестр пришёл НЕ из того файла, который правит мастер: слой выше
    (проектный `.gc/config.yml`, managed) переопределил `models`/`slots.default`.

    Считает тем же `origins`, что и витрина происхождения полей в Settings, —
    отдельного знания о слоях мастеру заводить не нужно.

    `ignore_profile` — финал мастера деактивирует профиль (ступень
    `without_profile` лестницы починки): перекрыть запись он уже не сможет, а
    `origins` судит по диску, где профиль ещё лежит. Не исключить его значит
    показать на одном экране два предупреждения, противоречащих друг другу
    (живой смок).

    «Исключить профиль» спрашивается СТЕКОМ БЕЗ ПРОФИЛЯ (`profile_override=""`,
    тем же приёмом, каким его снимает лестница), а не отбором готового ответа по
    виду слоя: `origins` называет ТОЛЬКО последний слой, изменивший значение, и
    профиль, ложась сверху, заслоняет собой проектный (managed), правящий те же
    ключи. Отбор выбрасывал их вместе с ним — плашка молчала, человек выбирал
    модель, мастер писал её в базу и снимал профиль, а работал слот из проектного
    слоя. Спрошенный без профиля стек называет реальный слой под ним."""
    if not config_path:
        return False
    from config_origin import origins
    found = origins(str(config_path), ["models", "slots.default"],
                    profile_override="" if ignore_profile else None)
    return any(origin.overlay_kind for origin in found.values())


def effective_model(config: Dict[str, Any]) -> str:
    """Действующая модель конфига — ПРОВАЙДЕРСКИЙ id из резолва `ModelRouter`
    (§6/§7), а не плоский `agent.model`: волна B пишет реестр (`models.<алиас>`
    + `slots.default`), а плоский ключ оставляет шаблонным сентинелом — по нему
    мастер на своём же настроенном конфиге видел бы «модель не задана» и молча
    переставлял слот на первую модель сервера.

    Канон витрин ветки (`config._effective_model_name`,
    `main._footer_model_name`), но берётся `model`, а не `alias`: обе поверхности
    мастера сверяются со списком С СЕРВЕРА, где модели названы провайдерскими id.

    Резолв не удался (полусырой конфиг: пустой реестр без плоского ключа, слот
    на несуществующий алиас) — фоллбэк на плоский ключ: мастер обязан
    открываться на любом конфиге. Сентинел шаблона — «ещё не задано», а не
    значение: отдаём пусто, как и на конфиге без модели вовсе."""
    name = config.get("model")
    try:
        from model_router import ModelRouter
        resolved = ModelRouter(
            config, level_provider=lambda: config.get("level") or "default",
        ).resolve("main", check_breaker=False)
        name = resolved.model
    except Exception:
        pass
    return "" if not name or name == MODEL_SENTINEL else name


def assigned_level_model(config: Dict[str, Any], level: str) -> str:
    """Провайдерский id модели, ЯВНО назначенной уровню, — или пусто, если слот
    не задан.

    Именно явный слот, а не резолв матрицей фоллбэков: незаданный `max` работает
    моделью `default`, но написать против него её имя значило бы сказать
    «уровень назначен» там, где он не назначен, — и следующий шаг закрепил бы
    выдуманное."""
    alias = (config.get("slots") or {}).get(level)
    if not alias:
        return ""
    entry = (config.get("models") or {}).get(alias)
    model = entry.get("model") if isinstance(entry, dict) else None
    return model or alias


def model_step_current(config: Dict[str, Any]) -> str:
    """Что шаг «Модель» называет текущим (пусто — «нечего показать»).

    Шаг задаёт слот `default` — про него и говорит. Не `effective_model`: та
    резолвит АКТИВНЫЙ уровень конфига, и при `level: max` строка «сейчас»
    показывала модель max с именем записи слота default, галочка в списке вставала
    не на ту строку, а сводка после подтверждения называла третью.

    Слота нет НИ ОДНОГО — резолв от уровня уже не зависит (легаси `agent.model` без
    реестра; единственная запись реестра, чей слот жил в отброшенном профиле, —
    живой стенд чинилки): показываем, чем работает агент, как и прежде. А вот при
    заданном другом уровне такой ответ снова зависел бы от `level`, и выдавать
    модель фоллбэка за назначенный `default` нельзя (то же правило, что у
    `assigned_level_model`) — строки тогда нет.

    Дом — здесь, потому что поверхностей две (плоский мастер и экран TUI), и
    вторая редакция этого правила разошлась бы с первой на первой же правке: ровно
    так дефект и жил — в плоском починен, в TUI остался."""
    current = assigned_level_model(config, "default")
    if not current and not any((config.get("slots") or {}).values()):
        current = effective_model(config)
    return current


def default_fallback_model(config: Dict[str, Any]) -> tuple:
    """`slots.default` пуст: чем уровень default работает СЕЙЧАС — (модель,
    подпись об источнике). Источник ищем тем же обходом матрицы `LEVEL_FALLBACK`
    (`config_modules/model_registry.py`), что и резолв: первый уровень после
    `default`, у которого задан слот, — и модель берём из ЕГО слота, а не из
    `effective_model`: та резолвит АКТИВНЫЙ уровень конфига, и при `level: light`
    с заданными обоими уровнями подпись называла max, а модель показывала light.
    Не нашёлся ни один (реестр из одной записи, например) — источник не
    выдумываем, пишем голый факт; резолв там от уровня не зависит.

    Дом — здесь: подпись печатают сводка плоского мастера и строка «сейчас» шага
    «Модель» обеих поверхностей (`model_step_display`) — вторая редакция обхода
    разошлась бы с первой на первой же правке."""
    from config_modules.model_registry import LEVEL_FALLBACK
    for level in LEVEL_FALLBACK["default"][1:]:
        model = assigned_level_model(config, level)
        if model:
            return model, f"(default не задан — работает {level})"
    return effective_model(config), "(default не задан)"


def model_step_display(config: Dict[str, Any]) -> tuple:
    """Строка «сейчас» шага «Модель»: (модель, подпись об источнике или пусто).

    Слот `default` задан (или слотов нет вовсе — прежнее правило
    `model_step_current`) — модель без подписи. Слоты есть, а `default` не задан
    — модель, которой default работает СЕЙЧАС по фоллбэку, С ПОДПИСЬЮ: пустая
    строка «сейчас» оставляла предвыбор редактора на ПЕРВОЙ модели сервера, и
    проход насквозь молча писал `slots.default` на неё — резолв default уезжал с
    рабочей модели фоллбэка на чужую (ревью M12). Предвыбор обеих поверхностей
    ведётся к модели этой строки: проход насквозь означает «оставить как есть».
    «Слот задан» подпись не утверждает — то же правило честности, что у
    `assigned_level_model`, только сказанное словами.

    Дом — здесь, по той же причине, что у `model_step_current`: поверхностей две,
    и вторая редакция правила уже расходилась с первой."""
    current = model_step_current(config)
    if current:
        return current, ""
    if any((config.get("slots") or {}).values()):
        return default_fallback_model(config)
    return "", ""


def overriding_layer_note(config: Dict[str, Any], config_path=None, *,
                          ignore_profile: bool = False) -> Optional[str]:
    """Спека §11: что перекроет выбор, сделанный в мастере (мастер пишет БАЗОВЫЙ
    config.yml, а конфиг собирается стопкой слоёв — ось 2, §2).

    Профиль (любой источник — базовый указатель, проектный, CLI) лежит над базой →
    предупреждаем. Реестр моделей — только когда он пришёл слоем ВЫШЕ правимого
    файла: свой же реестр config.yml мастер с волны B дописывает сам (решение 10),
    и предупреждать там не о чем, а проектный слой молча оставил бы выбор человека
    без эффекта. Без `config_path` слой не различить — тогда только профиль."""
    profile = config.get("profile") or None
    if profile and not ignore_profile:
        # Текст — общий для всех поверхностей (экран мастера и подсказка gc-cli),
        # поэтому это чистый факт без адреса действия: звать в «Настройки · Профили»
        # там, где TUI нет, значит врать. Адрес дописывает поверхность (спека §3).
        return f'действует профиль "{profile}" — он перекроет модельные настройки'
    if config.get("models") and _registry_overridden_above(config_path,
                                                            ignore_profile=ignore_profile):
        return ("действует реестр моделей из другого слоя конфигурации — ваш выбор "
                "ляжет в основной конфиг, но этот слой его перекроет")
    return None


def setup_guide(config_path: str, config: Dict[str, Any], program: str = "gc-cli",
                just_created: bool = False, *, repair: Optional["RepairLoad"] = None) -> str:
    """Подсказка по config_path.

    pending непуст → что вписать + как открыть помощник (`{program} --init-config`).
    pending пуст → позитивное «настроен». program — имя бинаря (gc-cli / gc-ua).
    just_created=True → конфиг создан этим запуском (иначе уже существовал).
    Если модель уже задана (а адрес/токен — нет), добавляем ремарку: эти поля
    нужны для preset-модели; либо укажи свою модель и адрес/токен под неё.

    `repair` — итог лестницы `load_config_for_repair`, если конфиг пришёл от
    неё (`--init-config` без TTY). На деградировавшей ступени `config` — не то,
    что соберёт следующий старт: без профиля «настроен — можно запускать» врало
    бы человеку, чей старт с профилем падает той же ошибкой (ревью), а на
    шаблоне перечисленные поля — поля шаблона, а не его файла. Гайд говорит и
    это. Ремарка о реестре из другого слоя судится без профиля (`ignore_profile`)
    ровно тогда, когда финал его СНИМЕТ (`repair.deactivate_profile`): второе
    предупреждение противоречило бы первому. Неснимаемый профиль перекрывать не
    перестанет — там ремарка остаётся.

    Путь показывается абсолютным (`_display_path`) — той же причиной, что и у
    `explain_broken_config`: относительный бессмыслен вне момента запуска.
    `config_path`, переданный дальше в `overriding_layer_note`, не трогаем —
    у неё своя (нечувствительная к форме пути) нормализация через
    `config_origin._overlay_paths`."""
    import os

    def _display_path(path) -> str:
        return os.path.abspath(path) if path else path

    stage = repair.stage if repair is not None else REPAIR_STAGE_FULL
    # Имя снятого профиля — под охраной: пустое (указатель есть, а имени за ним
    # нет) печаталось как `профиль "" сломан` и отправляло человека искать
    # несуществующий файл. Та же охрана — у соседнего `_repair_message`.
    named = f' "{repair.profile}"' if repair is not None and repair.profile else ""
    # Перекроет ли профиль выбор человека — вопрос к признаку деактивации, а не к
    # ступени: на ступени без профиля указатель бывает неснимаемым (лежит в
    # managed), и тогда профиль переживает запись и продолжает перекрывать реестр.
    # Тот же признак спрашивает вторая поверхность (экран мастера,
    # `tui/screens/onboarding.py::_layer_note`) — выведенный из ступени, он глушил
    # ремарку там, где экран её показывает.
    profile_note = overriding_layer_note(
        config, config_path,
        ignore_profile=repair.deactivate_profile if repair is not None else False)
    pending = pending_requirements(config)
    if not pending:
        if stage == REPAIR_STAGE_WITHOUT_PROFILE:
            base = (f"[gc] Обязательные поля заполнены: {_display_path(config_path)} — но "
                    f"профиль{named} сломан, и запуск с ним не поднимется: почините "
                    f"профиль или снимите указатель; запуск без него — "
                    f'{program} --profile "".')
        elif stage == REPAIR_STAGE_TEMPLATE:
            base = (f"[gc] config.yml не читается: {_display_path(config_path)} — показан "
                    f"пакетный шаблон; поправьте файл руками или сбросьте его: "
                    f"gc-cli --reset-config.")
        else:
            base = f"[gc] Конфиг настроен: {_display_path(config_path)} — можно запускать."
        return f"{base}\n[gc] {profile_note}." if profile_note else base
    fields = "\n".join(f"    • {r.hint}" for r in pending)
    note = ""
    if _legacy_model_set(config):
        # Ремарка — про ПЛОСКИЙ ключ (её текст его и называет): при живом реестре
        # с нетронутым плейсхолдером она печатала бы «agent.model = <ВПИШИТЕ_МОДЕЛЬ>».
        note = (
            f"\n  Модель по умолчанию уже задана (agent.model = {config.get('model')}) —\n"
            f"  осталось указать для неё адрес и токен; либо укажите свою модель\n"
            f"  и соответствующие адрес/токен.\n"
        )
    if stage == REPAIR_STAGE_WITHOUT_PROFILE:
        note += (
            f"\n  Профиль{named} сломан — запуск с ним не поднимется, пока он не\n"
            f'  починен или не снят (запуск без него: {program} --profile "").\n'
        )
    elif stage == REPAIR_STAGE_TEMPLATE:
        note += (
            "\n  Ваш config.yml не читается — поля выше показаны по пакетному шаблону,\n"
            "  а не по вашему файлу; сначала поправьте его руками или сбросьте:\n"
            "  gc-cli --reset-config.\n"
        )
    if just_created:
        header = "  GigaCode — конфигурация создана, настройка не завершена\n\n"
        location = f"  Создан файл конфигурации:\n    {_display_path(config_path)}\n\n"
    else:
        header = "  GigaCode — настройка не завершена\n\n"
        location = f"  Конфигурация:\n    {_display_path(config_path)}\n\n"
    if profile_note:
        note += f"\n  {profile_note}.\n"
    return (
        f"\n{header}{location}"
        f"  Не заполнены обязательные поля:\n{fields}\n"
        f"{note}\n"
        f"  Что дальше:\n"
        f"    • Помощник настройки (рекомендуется):\n"
        f"        {program} --init-config\n"
        f"    • Либо впишите поля вручную и запустите снова.\n\n"
        f"  MCP-серверы (веб-поиск, документы и др.): gc-mcp install — поставить, gc-mcp help — все команды\n"
    )


# --- Объяснение вместо трейсбека на нерезолвимом конфиге --------------------

# Подписи участников карты («Участвуют:») — kind из config_origin._overlay_paths
# плюс своя "base" для самой базы (её _overlay_paths не называет — она точка
# отсчёта, а не оверлей).
_BROKEN_CONFIG_LAYER_LABELS = {
    "base": "база",
    "managed": "managed",
    "project": "проект",
    "profile:project": "профиль",
    "profile:base": "профиль",
}


def _schema_error_keys(exc) -> List[str]:
    """Координаты ошибок схемы (pydantic `err["loc"]`) → dotted-пути, РЕЗОЛВИМЫЕ в
    документах слоёв, без повторов.

    `loc` заходит внутрь списков (`endpoint_presets.0.name`,
    `tool_usage_notice.2`), а поиск по документу идёт только ключами словарей
    (`config_raw.value_at`) — склеенная как есть такая координата не находится
    НИГДЕ, все слои выглядят пустыми, и виновником назначается база. Человеку
    тогда советуют откатить конфиг: откат подменит базу, потеряв его модели, MCP
    и голос, а сломанный оверлей останется на месте — то есть ломать целое ради
    чужой поломки. Обрезаем по первому нестроковому элементу: секция списка —
    самая точная координата, которую слои ещё умеют показать. Обрезка сливает
    соседей по одному списку в один путь, поэтому повторы отсеиваются: иначе
    строка совета называла бы одну и ту же секцию дважды."""
    keys: List[str] = []
    for err in exc.errors():
        parts: List[str] = []
        for part in err["loc"]:
            if not isinstance(part, str):
                break
            parts.append(part)
        key = ".".join(parts)
        if key and key not in keys:
            keys.append(key)
    return keys


def explain_broken_config(config_path: str, exc: Exception, *, program: str = "gc-cli",
                          profile_override: Optional[str] = None) -> str:
    """Человекочитаемое объяснение вместо трейсбека, когда `load_config_ex()` не
    смог собрать конфиг. `exc` — пойманный `ValueError` (pydantic `ValidationError`
    входит в него подклассом — единственный исход загрузки после защиты чтения
    базы, см. config.py/layers.py::_layer_document). `profile_override` — тот же,
    что ушёл в `load_config_ex` (`--profile`): объяснение обязано описывать стек,
    который грузила сессия, а не указатель из файла — иначе при `--profile broken`
    карта называла одну базу и советовала чинить в ней ключ, который верен
    (ревью). Имя из флага, не нашедшее файла, так и называется: чинить его во
    флаге, а не в файле и не откатом базы.

    Собирается из трёх частей: сам текст ошибки (pydantic-дамп приводится к тому
    же читаемому виду, что и на путях записи, — `layers.py::_app_config_from_raw`)
    + карта участвующих файлов (сырые документы, переживающие сломанный конфиг:
    `config_origin._overlay_paths` + `layers.resolve_profile_pointer`) + советы
    «что сделать» — от точечной починки к обходу и лишь затем к грубому откату
    (решение владельца: откат последний, он теряет модели, MCP и голос).

    Файл(ы) для починки называет общее с отказом записи ядро «названные ошибкой
    ключи → файлы, где они лежат»: координаты — `config_origin.error_keys`
    (закрытый список подозреваемых, координата — голова "; "-сегмента), файлы —
    `key_origins()` за ними. Разбор текста ОДИН на всё объяснение: те же
    координаты решают и совет про профиль из флага. Ошибка схемы координаты
    текстом не несёт — они в `loc` pydantic, и к резолвимому виду их приводит
    `_schema_error_keys`; дальше то же ядро, иначе дефект проектного оверлея
    приписывался базе (ревью). Не нашли ни одного ключа (битый YAML, нечитаемый
    файл) — сопоставлять нечего, совет становится «поправить файл: <путь>», где
    путь — тот, что называет сама ошибка (слои пишут «<path>: …», см.
    layers.py::_layer_document), либо базовый, если ни один известный файл в
    тексте не назван.

    Откат (`gc-cli --reset-config`) подменяет ОДНУ базу — советуется только когда
    виновата она: на сломанном оверлее или профиле он ничего не чинит, а модели,
    MCP и голос из базы теряет.

    Показанные пути — всегда абсолютные (`_display_path`): относительный, как
    из `--config .\\config.yml`, бессмыслен вне момента запуска (другой cwd,
    копипаста в переписку) и рядом с уже абсолютным путём профиля читается
    неряшливо. Нормализация — только на выходе, в f-строках ниже: `config_path`
    и всё, что от него посчитано (`participants`, `named`, `culprit.path`),
    остаются НЕТРОНУТЫМИ — на них стоит сравнение `error_text.startswith(...)`,
    и здесь важно опознать файл ТЕМ же видом пути, каким его назвала сама
    ошибка (см. `_layer_document`/`validate_registry_pairs`, которые пишут путь
    без прогона через `os.path.abspath`)."""
    import os

    from pydantic import ValidationError as _ValidationError

    from config_modules.layers import resolve_profile_pointer
    from config_modules.root import SCHEMA_ERROR_PREFIX, schema_error_details
    from config_origin import _overlay_paths, error_keys, key_origins

    def _display_path(path) -> str:
        return os.path.abspath(path)

    flagged = profile_override is not None
    active = profile_override if flagged else resolve_profile_pointer(config_path)
    participants = ([("base", config_path)]
                    + _overlay_paths(config_path, profile_override=profile_override))
    # Имя из флага НЕ резолвится в файл — единственное, о чём вправе говорить совет
    # «такого профиля нет». Признаком служил один лишь факт флага, и на сломанном
    # САМОМ указателе (`profile: 123` в базе — ошибка схемы) совет объявлял
    # несуществующим профиль, который лежит на месте и исправен, а виноватый файл
    # не назывался вовсе: под override `key_origins` выбрасывает ключ `profile`, и
    # виновников не остаётся. Пустой `active` (`--profile ""`) — профиля не
    # запрашивали, и «такого профиля нет» не про что.
    flagged_profile_missing = bool(flagged and active) and not any(
        kind.startswith("profile:") for kind, _ in participants)
    participant_lines = [
        f"    {_BROKEN_CONFIG_LAYER_LABELS[kind].ljust(10)}{_display_path(path)}"
        + (("   (активен: --profile)" if flagged else "   (активен)")
           if kind.startswith("profile:") else "")
        for kind, path in participants
    ]

    # Координаты названных ошибкой ключей — ОДИН разбор на всё объяснение: их
    # спрашивают и виновники, и совет про профиль из флага. Второе чтение той же
    # строки — второе прочтение одного правила, и разъезжаются они молча.
    # Загрузка (`config.load_config_ex`) отдаёт отказ схемы уже завёрнутым —
    # ValueError с текстом без pydantic-дампа (`input_value=<repr записи>` нёс
    # токен из файла; MR #80, класс L28), а сам `ValidationError` едет в
    # `__cause__`: его `loc` точнее текста (заходит в списки и не ограничен
    # закрытым списком подозреваемых `error_keys`), и виновники ищутся по нему.
    # Сырой `ValidationError` первым аргументом — страховка на прямых вызовах.
    schema_exc = exc if isinstance(exc, _ValidationError) else (
        exc.__cause__ if isinstance(exc.__cause__, _ValidationError) else None)
    if schema_exc is not None:
        error_text = (str(exc) if schema_exc is not exc
                      else f"{SCHEMA_ERROR_PREFIX}{schema_error_details(schema_exc)}")
        keys = _schema_error_keys(schema_exc)
    else:
        error_text = str(exc)
        keys = error_keys(error_text)
    culprits = key_origins(config_path, keys, profile_override=profile_override)

    fix_lines: list[str] = []
    base_to_blame = any(culprit.kind == "base" for culprit in culprits)
    if culprits:
        for culprit in culprits:
            fix_lines.append(
                f"поправить {', '.join(culprit.keys)} в:  {_display_path(culprit.path)}")
    elif flagged_profile_missing and "profile" in keys:
        # Ошибка про сам указатель, а он пришёл из флага: файла-виновника нет
        # (не найден или зарезервирован — в обоих случаях такого профиля нет).
        fix_lines.append(
            f'профиль "{active}" задан флагом --profile — такого профиля нет, '
            f"исправить имя во флаге")
    else:
        named = config_path
        for _, candidate in participants:
            if error_text.startswith(f"{candidate}:"):
                named = candidate
                break
        base_to_blame = named == config_path
        fix_lines.append(f"поправить файл:  {_display_path(named)}")

    # Советы-команды целятся в ФАЙЛ СЕССИИ, а не в дефолтный (ревью M9): при
    # `--config <путь>` фиксированный `gc-cli --reset-config` сбрасывал ЧУЖОЙ
    # исправный `~/.gc/config.yml`, оставляя сломанный на месте, а `--profile ""`
    # запускал не тот конфиг. Кавычки — пробелы в путях; сравнение — normcase от
    # абсолютного вида (`--config .\\config.yml` называет тот же файл, Windows
    # к регистру нечувствителен).
    from config import _default_config_path, user_config_path

    def _same_path(a, b) -> bool:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
            os.path.abspath(str(b)))

    config_flag = ("" if _same_path(config_path, user_config_path())
                   else f' --config "{_display_path(config_path)}"')
    if active:
        fix_lines.append(f'запустить без профиля, ничего не потеряв:  '
                         f'{program}{config_flag} --profile ""')
    # Пакетному шаблону сброс не предлагается вовсе: `--reset-config` его не
    # правит и честно откажет — совет вёл бы в тупик.
    if base_to_blame and not (config_flag
                              and _same_path(config_path, _default_config_path())):
        fix_lines.append(f"откатить конфиг к шаблону (старый уйдёт в бэкап):  "
                         f"gc-cli{config_flag} --reset-config")

    error_block = "\n".join(f"  {line}" for line in error_text.splitlines())
    fix_block = "\n".join(f"    • {line}" for line in fix_lines)
    participants_block = "\n".join(participant_lines)

    return (
        f"[gc] Конфиг не загружается: {_display_path(config_path)}\n\n"
        f"{error_block}\n\n"
        f"  Участвуют:\n{participants_block}\n\n"
        f"  Что сделать:\n{fix_block}\n"
    )


# --- Загрузка конфига «чинилками» (--init-config) --------------------------

REPAIR_STAGE_FULL = "full"
REPAIR_STAGE_WITHOUT_PROFILE = "without_profile"
REPAIR_STAGE_TEMPLATE = "template"


class RepairLoad(NamedTuple):
    """Итог лестницы `load_config_for_repair`: конфиг, метаданные профиля,
    ступень приземления и готовое сообщение человеку (None — деградации не было).

    `profile` — имя профиля, СНЯТОГО лестницей (ступень `without_profile`; иначе
    пусто — на ступени `full` профиль активен, но не снимался): поверхности
    называют его, не разбирая текст сообщения.

    `deactivate_profile` — СОТРЁТ ли указатель этого профиля запись, которую
    сделает финал мастера (`write_wizard_values(deactivate_profile=...)`). True
    только на ступени `without_profile` и только когда `profile: ""`, легший по
    правилу адреса записи (`layers.profile_pointer_write_target`), действительно
    обнуляет указатель для следующего старта. Признак, выведенный из ВИДА слоя,
    отвечал не на тот вопрос: указатель, названный двумя слоями одинаково,
    приписан первому из них, а при чтении побеждает последний. Не сотрёт
    (указатель держит слой, в который правило адреса писать не станет, — managed:
    снятие ушло бы в базу, а managed поднимал бы тот же сломанный профиль, и гейт
    отверг бы запись, ревью D3) — тогда False, и сообщение называет файл.
    Обе точки входа (`cli._run_init`, `tui.main`) берут ИМЕННО это поле: признак,
    выведенный из одной лишь ступени, обещал человеку деактивацию, которой не
    будет, и успевал положить пустой якорь профиля в базу."""
    config: Dict[str, Any]
    meta: Any                      # ProfileMeta загрузчика
    stage: str
    message: Optional[str]
    profile: str = ""              # умолчания = «ничего не снималось» (ступень full)
    deactivate_profile: bool = False


class RepairRefused(Exception):
    """Честный отказ чинилки ДО вопросов (ревью M10): ступень шаблона при живом
    указателе `profile:`.

    Запись мастера здесь обречена целиком: гейт записи судит стек СЛЕДУЮЩЕГО
    старта, а в нём указатель поднимает профиль поверх базы, которая не
    собирается и без него, — прогон ревьюера: человек отвечает на все вопросы,
    вводит токен, а запись отвергается («конфиг не записан»), md5 файла не
    меняется. «Просто снять указатель» финалом не работает: снятие — отдельная
    запись с той же фазой 2, и без профиля база не собирается тоже. Один
    транзакционный акт (указатель + реестр + плоские поля одним валидируемым
    кандидатом) требует либо второй редакции сборки YAML вне писателей, либо
    смены порядка записей, при котором токен ложится на диск до вердикта
    реестра, — оба пути отвергнуты, отказ честнее.

    НЕ ValueError намеренно: на ValueError точки входа печатают
    `explain_broken_config`, и отказ утонул бы в чужом объяснении с советом
    `--profile ""`, который здесь не помогает. Текст исключения — готовое
    сообщение человеку: печатать как есть, код 1."""


def _refusal_message(exc: Exception, profile: str, base_path, pointer_path) -> str:
    """Текст отказа `RepairRefused` — один на обе точки входа (stderr gc-cli и
    stderr gc-ua до создания TuiApp). Советы — от точечного к грубому (правило
    `explain_broken_config`); `--profile ""` не советуем: база не грузится и без
    профиля, флаг тупика не снимает — а вот снять указатель В ФАЙЛЕ можно, и это
    ровно тот шаг, после которого мастер способен дописать починку (прогон
    ревьюера). Пути — абсолютные, по той же причине, что у
    `explain_broken_config`."""
    import os
    base_shown = os.path.abspath(str(base_path))
    pointer_shown = os.path.abspath(str(pointer_path))
    return (
        f"[gc] Мастер эту раскладку не починит: config.yml не читается и без "
        f'профиля ({exc}), а профиль "{profile}" останется активным — стек '
        f"следующего старта отвергнет запись мастера целиком.\n"
        f"  Что сделать:\n"
        f"    • поправить файл руками:  {base_shown}\n"
        f'    • снять указатель профиля (строкой profile: "") в:  {pointer_shown} '
        f"— и запустить мастер снова\n"
        f"    • либо откатить конфиг к шаблону (старый уйдёт в бэкап):  "
        f"gc-cli --reset-config"
    )


def _repair_message(stage: str, exc: Exception, profile: str,
                    pinned_by: Optional[str] = None) -> str:
    """Текст деградации — ОДИН на все поверхности (stderr gc-cli, stderr gc-ua без
    TTY, runtime-notice мастера). Говорит не только «сломано», но и что именно
    отброшено: человек обязан понимать, почему поля мастера выглядят иначе, чем
    его файл.

    `pinned_by` — путь managed-файла, если указатель лежит в нём: деактивацию
    тогда не обещаем (см. `RepairLoad.deactivate_profile`), а говорим, где
    указатель и что снять его нужно там.

    Ступень шаблона обещает ровно то, что делает писатель: ответы ложатся в файл
    точечно, по якорям (`onboarding_config_writer`), целиком файл не
    переписывается — поломка, которой ответы не касаются, остаётся, и
    неразбираемый файл отвергает запись (ревью M8: текст «перепишет ваш файл»
    вёл человека через все вопросы к отказу записи)."""
    if stage == REPAIR_STAGE_WITHOUT_PROFILE:
        named = f' "{profile}"' if profile else ""
        text = (
            f"config.yml не читается с профилем{named} ({exc}) — профиль снят на этот "
            f"запуск, остальные ваши настройки (адрес, токен, реестр моделей) действуют."
        )
        if pinned_by:
            text += (
                f" Указатель на профиль лежит в managed-файле {pinned_by} — мастер его "
                f"не снимет, снять нужно там."
            )
        return text
    return (
        f"config.yml не читается ({exc}) — продолжаю на пакетном шаблоне: ваших "
        f"настроек в мастере не видно. Ответы лягут в ваш файл точечно, целиком он "
        f"не переписывается, и поломка, которой они не касаются, останется — тогда "
        f"поправьте файл руками или сбросьте его: gc-cli --reset-config."
    )


def load_config_for_repair() -> RepairLoad:
    """Загрузка конфига для «чинилки» (`gc-cli --init-config`, `gc-ua --init-config`)
    — лестница сверху вниз: полная загрузка → БЕЗ ПРОФИЛЯ → пакетный шаблон.

    Дом — здесь, а не в `config.py`, потому что деградация загрузки — свойство
    ПЕРВОГО ЗАПУСКА и починки, а не загрузчика: обычный старт обязан падать громко
    (`explain_broken_config` + код 1), и лестница рядом с ним в config.py читалась
    бы как «конфиг всегда как-нибудь да загрузится». Здесь же живёт родня —
    политика «настроен ли конфиг» и весь текст, который эти же две поверхности
    показывают человеку.

    Ступень 2 — не украшение: поломка почти всегда сидит в профиле (слот на
    несуществующий алиас), а не в базе, и падение сразу на шаблон стоило человеку
    рабочих адреса и токена — мастер уходил в сеть на `your-backend:PORT` и не
    получал список моделей при живом токене.

    Ступень 3 отдаёт ШАБЛОН, а не пустой `{}`: рантайм TUI требует полей вроде
    `threads_data_path`, и на пустом словаре `resolve_threads_dir` падает сразу
    следом. Шаблон валиден по построению; мастер всё равно пишет в
    ПОЛЬЗОВАТЕЛЬСКИЙ файл, а не в него.

    Про поломку рассказывает ПЕРВАЯ ошибка (та, что человек видит на обычном
    старте), а не отказ промежуточной ступени: вторая — диагноз уже урезанного
    конфига.

    Все три ступени накладывают верхние слои (managed, проектный `.gc/config.yml`)
    — на сломанном оверлее не собирается и шаблон. Тогда лестница исчерпана и
    наверх уходит та же ПЕРВАЯ ошибка (`ValueError`), а не отказ ступени шаблона:
    мастер пишет в пользовательский файл, а сломан чужой слой — чинить его
    мастером нечем, и вызывающий обязан объяснить поломку тем же
    `explain_broken_config`, что и обычный старт (код 1, не трейсбек — ревью).
    Не четвёртая ступень с пустым конфигом: конфига нет, и молчаливый `None`
    в `RepairLoad.config` доехал бы до `resolve_threads_dir` следом."""
    from config import _default_config_path, load_config_ex, resolve_config_path

    try:
        config, meta = load_config_ex(None)
        return RepairLoad(config, meta, REPAIR_STAGE_FULL, None, "", False)
    except ValueError as exc:
        broken = exc

    try:
        config, meta = load_config_ex(None, profile_override="")
    except ValueError:
        try:
            config, meta = load_config_ex(_default_config_path(), profile_override="")
        except ValueError:
            raise broken from None
        # Живой указатель на ступени шаблона — тупик записи (M10): гейт судит
        # стек следующего старта, где профиль лежит поверх несобираемой базы, и
        # отвергнет всё, что мастер соберёт (включая отдельное снятие
        # указателя). Отказываем ДО вопросов; файл указателя называет та же
        # витрина происхождения, что у ступени «без профиля» ниже. Резолв
        # указателя best-effort (битую базу прощает): пустой ответ значит
        # «профиля нет нигде в стеке» — тогда мастер работает по контракту
        # ступени шаблона, отказывать не в чем.
        from config_modules.layers import resolve_profile_pointer
        from config_origin import origins
        base_path = resolve_config_path(None)
        profile = resolve_profile_pointer(base_path)
        if profile:
            survivor = origins(base_path, ["profile"], profile_override="")["profile"]
            pointer_path = survivor.overlay_path or str(base_path)
            raise RepairRefused(
                _refusal_message(broken, profile, base_path, pointer_path)) from broken
        return RepairLoad(config, meta, REPAIR_STAGE_TEMPLATE,
                          _repair_message(REPAIR_STAGE_TEMPLATE, broken, ""), "", False)

    from config_modules.layers import profile_pointer_write_target, resolve_profile_pointer
    from config_origin import origins

    base_path = resolve_config_path(None)
    profile = resolve_profile_pointer(base_path)
    # Сотрёт ли указатель запись финала мастера — спрашивается ПОДСТАНОВКОЙ: в стек
    # кандидатом кладётся документ адреса записи со снятым указателем, и тот же
    # резолв, каким указатель читает следующий старт, отвечает, что от него
    # останется. Вопрос «чей слой назвал профиль» (`origins`) на это не отвечает:
    # он приписывает значение ПЕРВОМУ слою, задавшему его, а при чтении побеждает
    # ПОСЛЕДНИЙ — указатель, названный managed и повторённый проектным слоем,
    # выглядел неснимаемым, хотя запись легла бы в проектный и сработала.
    target = profile_pointer_write_target(base_path)
    erased = dict(config_raw.read_document(target))
    erased["profile"] = ""
    pinned_by = None
    if resolve_profile_pointer(base_path, candidates={target: erased}):
        # Указатель запись переживёт — человеку нужен файл, где он лежит: слой
        # называет та же витрина происхождения, спрошенная по тому же стеку.
        survivor = origins(base_path, ["profile"], candidates={target: erased},
                           profile_override="")["profile"]
        pinned_by = survivor.overlay_path or str(base_path)
    return RepairLoad(config, meta, REPAIR_STAGE_WITHOUT_PROFILE,
                      _repair_message(REPAIR_STAGE_WITHOUT_PROFILE, broken, profile, pinned_by),
                      profile, pinned_by is None)


# --- Запись собранного мастером (волна B, спека §11) ----------------------

def default_alias_for(model_id: str) -> str:
    """Алиас `models.<alias>` из провайдерского id модели: последний path-сегмент
    до опций (`qwen/qwen-2.5-flash` → `qwen-2.5-flash`; `qwen/m:free` → `m`),
    короткое имя без `/` — как есть. Имени алиаса мастер не спрашивает: на первом
    запуске лишний вопрос дороже, чем предсказуемое имя, а переименовать запись
    можно в секции «Модели».

    Коллизия с зарезервированным словом уровня (`RESERVED_WORDS`, §4) → суффикс
    `-model` (`light` → `light-model`): иначе запись не прошла бы валидацию.
    Пустой id (или после нормализации — пустая строка) → `"model"`.
    """
    from config_modules.model_registry import RESERVED_WORDS

    if not model_id:
        return "model"
    slug = model_id.strip().rsplit("/", 1)[-1].split(":", 1)[0].strip()
    if not slug:
        return "model"
    return f"{slug}-model" if slug in RESERVED_WORDS else slug


def unique_alias(alias: str, taken) -> str:
    """`alias`, либо `alias-2`/`alias-3`/… — первое имя, не занятое в `taken`.

    Нужен шагу уровней: два РАЗНЫХ провайдерских id могут свернуться в одно имя
    (`vendor-a/qwen-plus` и `vendor-b/qwen-plus`), и без разведения вторая запись
    затёрла бы первую по merge-контракту — оба слота увели бы уровни на одну
    модель, а выбор человека потерялся бы молча."""
    taken = set(taken)
    if alias not in taken:
        return alias
    n = 2
    while f"{alias}-{n}" in taken:
        n += 1
    return f"{alias}-{n}"


# Модели, собранные мастером: ключ в `values` → слот реестра. `model` — базовый шаг,
# `model_light`/`model_max` — опциональный скипаемый шаг уровней (TUI, решение 8
# плана волны B). Имена ключей — контракт между шагами и записью, поэтому живут
# здесь, у политики записи, а не в экране.
MODEL_SLOT_KEYS: Dict[str, str] = {
    "default": "model",
    "light": "model_light",
    "max": "model_max",
}


def _aliases_in_file(config_path) -> Dict[str, str]:
    """Алиасы реестра, УЖЕ лежащие в правимом файле → провайдерский id каждого
    (`model` не задан — id равен самому алиасу, §4).

    Нужны уникализации имён: `upsert_model` МЕРЖИТ в существующую запись, поэтому
    занятое имя — это чужая пара «адрес + токен», которая переживёт прогон. Читаем
    именно правимый файл, а не merged-конфиг: домержится запись только здесь.

    Файл нечитаем или не парсится — считаем реестр пустым: сказать об этом человеку
    всё равно нечем, а следом идёт `apply_registry_ops`, который тот же файл читает
    сам и отказывает понятной ошибкой."""
    from pathlib import Path

    import yaml

    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        return {}
    return {alias: ((entry or {}).get("model") if isinstance(entry, dict) else None) or alias
            for alias, entry in models.items()}


def _registry_ops(values: Dict[str, Any], existing: Optional[Dict[str, str]] = None
                  ) -> List[tuple]:
    """Операции реестра для собранных мастером моделей: `models.<alias>` + свой
    слот на каждый ЗАПОЛНЕННЫЙ уровень (незаполненный не пишет ничего — слот на
    несуществующий алиас нерезолвим, §4).

    Один и тот же id на двух уровнях — одна запись и два слота на неё; разные id
    с одинаковым хвостом имени разводятся `unique_alias`. Соответствие «id → уже
    выданный алиас» ищется по РЕЗУЛЬТАТУ, а не по первому расчёту имени: иначе
    третий уровень с уже разведённой моделью получал бы ещё одну запись-близнеца.

    `existing` (алиас → id, уже лежащие в файле — `_aliases_in_file`) участвует в
    той же уникализации: имя за ТОЙ ЖЕ моделью переиспользуется (upsert домержится
    в неё, и тариф записи переживает повторный прогон), а имя за ДРУГОЙ моделью
    занято — взяв его, мастер увёл бы слот на чужую пару «адрес + токен», хотя
    человек только что ввёл новое подключение."""
    ops: List[tuple] = []
    existing = existing or {}
    planned: Dict[str, str] = {}          # алиас → id, уже запланированный этой записью
    for slot, key in MODEL_SLOT_KEYS.items():
        model_id = values.get(key)
        if not model_id:
            continue
        # Имя ищется и среди УЖЕ ЛЕЖАЩИХ в файле, а не только среди
        # запланированного этой записью: иначе повторный прогон мастера не узнавал
        # записи, названные человеком (`fast`, `brain`), и заводил рядом близнецов
        # под расчётным именем. Слоты уезжали на новые записи — без своей пары
        # «адрес и токен», — а прежние оставались осиротевшими: уровень, ходивший
        # на локальный сервер, уходил на общий endpoint с id локальной модели.
        # Порядок словарей важен: своё запланированное имя побеждает существующее.
        alias = next((name for name, known_id in {**existing, **planned}.items()
                      if known_id == model_id), None)
        if alias is None:
            alias = default_alias_for(model_id)
            if alias in planned or existing.get(alias, model_id) != model_id:
                alias = unique_alias(alias, set(planned) | set(existing))
            planned[alias] = model_id
            ops.append(("upsert_model", alias, {"model": model_id}))
        ops.append(("write_slot", slot, alias))
    return ops


class PartialWriteError(ValueError):
    """Отказ записи мастера, часть которой уже на диске.

    `written` — имена состоявшихся записей в порядке исполнения:
    `"profile_pointer"` (снятие указателя) и `"registry"` (транзакция реестра).
    Что успело лечь, знает только писатель, и поверхности мастера обязаны читать
    ответ здесь: выведенный из порядка записи состав разъедется с писателем на
    первой же правке порядка — человек прочтёт про снятый профиль там, где его не
    снимали. Голый `ValueError` — противоположный ответ: не легло НИЧЕГО.

    Текст исключения — сам отказ, без служебных префиксов: обе поверхности
    печатают его как есть.

    Подкласс `ValueError`: вызывающие ловят его прежними `except ValueError`, и
    ветку «конфиг не записан» разбирают те, кому есть что сказать иначе."""

    def __init__(self, message: str, *, written) -> None:
        super().__init__(message)
        self.written = tuple(written)


def write_wizard_values(config_path, values: Dict[str, Any], *,
                        deactivate_profile: bool = False) -> List[str]:
    """Записать собранное мастером в `config_path` — ОДИН путь записи на обе
    поверхности (CLI-визард и TUI-мастер), политика живёт здесь.

    Подключение (`completions_url`/`Authorization`/`proxy`) пишется плоскими
    ключами `backend.*`, КАК РАНЬШЕ: это база наследования записей реестра (§4) и
    живой источник пробы соединения. Модели — записями реестра `models.<alias>:
    {model: <id>}` + своими слотами вместо легаси `agent.model` (спека §11):
    базовый шаг даёт `slots.default`, опциональный шаг уровней — `slots.light`/
    `slots.max`. Per-model `url`/`token` мастер НЕ пишет намеренно: в режиме
    `responses` они не действуют (§4), и запись, самодостаточная в
    `chat_completions`, увела бы responses-конфиг с рабочего endpoint'а.

    Все записи реестра уходят ОДНОЙ транзакцией: вторая запись без слота
    нерезолвима (§4) и была бы отвергнута валидацией отдельной операцией.

    Записей всё же ДВЕ (реестр и плоские `backend.*`), и порядок между ними —
    часть политики: реестр идёт ПЕРВЫМ. Его отказ тогда не пишет ничего, и «конфиг
    не записан» — правда, включая секрет: плоским ключом уходит ТОКЕН, и оставить
    его на диске от несостоявшегося прогона нельзя. Отказ второй записи оставляет
    модели без подключения — `is_unconfigured` вернёт человека в мастер, а секрета
    на диске нет.

    `deactivate_profile` — лестница приземлилась на ступень
    `REPAIR_STAGE_WITHOUT_PROFILE`: конфиг собрался ТОЛЬКО потому, что профиль сняли
    на этот запуск. Тогда финал снимает и указатель — иначе чинилка не может
    записать собственную починку: обе записи ниже судятся по стеку СЛЕДУЮЩЕГО
    старта, а там указатель снова поднимает сломанный профиль, и запись честно
    отвергается («slots.default: алиас ... не найден» — живой тупик стенда). Файл
    профиля не трогаем: человек чинит его позже и включает обратно.

    Снятие указателя идёт ПЕРЕД обеими записями, и это не вопрос удобства. Порядок
    наоборот невозможен физически: пока указатель на месте, гейт отвергает всё, что
    мастер собрал. А частичный исход честен: ступень `without_profile` значит, что
    без профиля база грузится, — то есть после одного лишь снятия конфиг ПОДНИМЕТСЯ
    (было: не грузился вовсе), настройки человека останутся прежними и рабочими, а
    токен этого прогона на диск ещё не уходил.

    Возвращает якоря плоских полей, которых в конфиге не нашлось (вызывающий
    решает, показывать ли предупреждение); отказ записи — `ValueError`, а когда
    часть уже легла — `PartialWriteError` с именами состоявшихся записей.
    """
    import onboarding_config_writer as ocw
    from config import _default_config_path, validate_config_text
    from config_modules.layers import profile_pointer_write_target
    from config_modules.registry_write import apply_registry_ops
    from utils.user_text import read_user_text_or_none

    # Что успело лечь — счёт ведётся ЗДЕСЬ, по факту состоявшихся записей: только
    # этот код знает порядок, и только он вправе о нём рассказывать
    # (`PartialWriteError.written`).
    landed: List[str] = []
    if deactivate_profile:
        # Куда ложится `profile: ""` и как — общие с секцией «Профили» правила
        # (`profile_pointer_write_target` + `ocw.write_profile_pointer`): своя
        # редакция писала бы в слой, который следующий старт всё равно перекроет.
        ocw.write_profile_pointer(
            profile_pointer_write_target(config_path), "", base_path=config_path)
        landed.append("profile_pointer")
    template_path = _default_config_path()
    # Шаблон недоступен (нет файла, нет прав, чужая кодировка) — это не повод
    # уронить мастер трейсбеком: якорь отсутствующей секции писатель синтезирует
    # сам (`ensure_section_anchor_text`), а плоскому ключу без якоря останется
    # отчёт «не записан». Путь недочитанного шаблона дальше не отдаём: тот же
    # отказ ждал бы и `write_values`, только уже трейсбеком.
    template = read_user_text_or_none(template_path) or ""
    model_keys = set(MODEL_SLOT_KEYS.values())
    flat = {FIELD_ANCHOR_PATHS.get(key, key): value
            for key, value in values.items() if key not in model_keys}
    ops = _registry_ops(values, _aliases_in_file(config_path))
    # Форма отказа — по одному признаку: легло ли уже что-нибудь. Не легло ничего
    # (отказала самая первая запись) — исключение уходит наружу как было, и «конфиг
    # не записан» тогда правда. Легло — правдой это уже не будет, и молча выдать
    # одно за другое значит отправить человека искать нетронутый файл, где лежит
    # новый реестр (а на ступени без профиля — ещё и снятый указатель).
    # OSError — штатный отказ всех трёх записей (диск, права, win-лок открытого
    # файла), а не невозможный случай: мимо перехвата он уходил голым трейсбеком
    # (CLI) и падением Textual (TUI), при уже лежащих на диске `profile: ""` и
    # реестре — снятый указатель не назывался нигде (ревью L33). Форма ответа та
    # же, что у ValueError: legло что-то — PartialWriteError, нет — как есть.
    if ops:
        try:
            apply_registry_ops(config_path, ops, template)
        except (ValueError, OSError) as exc:
            if landed:
                raise PartialWriteError(str(exc), written=landed) from exc
            raise
        landed.append("registry")
    try:
        return ocw.write_values(config_path, flat,
                                template_path=template_path if template else None,
                                validate=validate_config_text)
    except (ValueError, OSError) as exc:
        if landed:
            raise PartialWriteError(str(exc), written=landed) from exc
        raise
