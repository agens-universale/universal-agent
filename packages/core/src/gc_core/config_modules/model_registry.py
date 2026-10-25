"""Секции конфига `models`/`slots`/`assign` — реестр моделей, уровни, привязки потребителей.

Фаза 1 (эта фаза): только схема + пофайловые валидаторы (типы, зарезервированные
имена, конфликты дублирующих настроек одной модели). Ссылочная целостность
(слот → существующий алиас, привязка → уровень/алиас из merged-реестра) — фаза 2,
вне этого модуля.

Как это настраивается пользователем — docs/configuration.md, раздел
«Профили и уровни моделей».
"""

from __future__ import annotations

import copy
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from gc_core.config_modules.backend import ModelBackendConfig
from gc_core.config_modules.chat_completions import (
    auxiliary_llm_from_config_dict,
)
from gc_core.config_modules.env_interpolation import (
    _FILE_MARKER,
    resolve_env_only,
)
from gc_core.config_modules.field_meta import SecretText, gc_meta, secret_value
from gc_core.config_modules.pricing import ModelTariff

LEVELS = ("light", "default", "max")
#: Допустимые значения `level` и флагов `--level`: уровни и режим автовыбора.
LEVEL_VALUES = (*LEVELS, "auto")
RESERVED_WORDS = LEVEL_VALUES

# Транспорт одной модели поверх общего backend. Ключ в поле — переписывает
# одноимённый дефолт; незаписанный остаётся общим. Сэмплинг сюда не входит.
MODEL_BACKEND_OVERLAY_KEYS = (
    "request_timeout_sec",
    "proxy",
    "use_ssl",
    "cert_path",
    "key_path",
    "use_retry",
    "retry_embedded_choice_errors",
    "chat_completions_max_tokens",
    "chat_completions_min_response_tokens",
    "use_max_tokens_param",
    "openai_strict_tools_schema",
    "send_tool_strict",
    "claude_prompt_cache",
    "vision",
    "chat_completion_headers",
    "completions_url",
)
# Поля BackendConfig, которые рантайм читает только из общего backend.
MODEL_BACKEND_REJECTED = {
    "chat_completion_kwargs": (
        "температура и thinking задаются в main и auxiliary"
    ),
    "api_mode": "режим API задаётся в общем backend",
    "stream": "stream задаётся в общем backend",
}

# Рантайм ставит их сам (модель, история, тулы, лимит ответа). В main/auxiliary
# они не пишутся: воронка их режет, форма отказывает до записи.
PROTECTED_COMPLETION_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "max_tokens",
        "max_completion_tokens",
    }
)

# Матрица фоллбэков уровня (спека §4): порядок обхода слотов, если слот
# запрошенного уровня не задан. Живёт рядом с LEVELS — потребитель (ModelRouter,
# транспортный слой) не должен ради трёх кортежей тянуть всё дерево AppConfig.
LEVEL_FALLBACK: dict[str, tuple[str, str, str]] = {
    "light": ("light", "default", "max"),
    "default": ("default", "max", "light"),
    "max": ("max", "default", "light"),
}


def _parse_mapping_text(text: str) -> dict[str, Any] | None:
    """Синтаксис полей модели; ошибка без содержимого строки с возможным секретом."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = (
            f" (строка {mark.line + 1}, столбец {mark.column + 1})"
            if mark
            else ""
        )
        raise ValueError(f"неверный YAML{where}") from exc
    if loaded is not None and not isinstance(loaded, dict):
        raise ValueError("нужен YAML-mapping, не список и не скаляр")
    return loaded


def parse_completion_kwargs_text(text: str) -> dict[str, Any] | None:
    """Текст поля ``main``/``auxiliary`` → mapping или отсутствие mapping.

    Пустое поле, YAML ``null`` / ``~`` и текст из одних комментариев — ``None``:
    способ записи отсутствия определяет редактор (снять ключ или явный null).
    Явный пустой mapping (``{}``) — ``{}``: в запрос эти ключи не класть.
    JSON object — валидный YAML, разбирается тем же путём. Список и скаляр —
    ошибка. Protected-ключи — ошибка: в файл их не кладут, рантайм всё равно
    вырежет."""
    raw = text.strip()
    if not raw:
        return None
    loaded = _parse_mapping_text(raw)
    if loaded is None:
        return None
    forbidden = [
        str(key) for key in loaded if key in PROTECTED_COMPLETION_KEYS
    ]
    if forbidden:
        raise ValueError(
            f"ключи {', '.join(forbidden)} задаёт рантайм — уберите их из поля"
        )
    return loaded


def _reject_disallowed_backend_keys(loaded: dict) -> None:
    """Ключи, которые форма принимает как YAML, но рантайм с модели не читает."""
    rejected = [str(key) for key in loaded if key in MODEL_BACKEND_REJECTED]
    if rejected:
        details = "; ".join(
            f"{key}: {MODEL_BACKEND_REJECTED[key]}" for key in rejected
        )
        raise ValueError(details)
    unknown = [
        str(key) for key in loaded if key not in MODEL_BACKEND_OVERLAY_KEYS
    ]
    if unknown:
        allowed = ", ".join(MODEL_BACKEND_OVERLAY_KEYS)
        raise ValueError(
            f"неизвестные ключи: {', '.join(unknown)}. Допустимы: {allowed}"
        )


def _validated_backend_overlay(loaded: dict) -> dict[str, Any]:
    """Проверить копию, сохранить исходные ссылки и значения.

    FILE-поля проверит загрузчик при записи транзакции; редактор не читает
    файлы на каждое изменение текста. ENV раскрывается без дискового I/O.
    """
    headers = loaded.get("chat_completion_headers")
    if headers is not None and not isinstance(headers, dict):
        raise ValueError("chat_completion_headers: нужен YAML-mapping")
    effective = resolve_env_only(loaded)
    immediate = {
        key: value
        for key, value in effective.items()
        if not (isinstance(value, str) and _FILE_MARKER.search(value))
    }
    try:
        ModelBackendConfig.model_validate(immediate)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(part) for part in err.get("loc") or ())
        msg = err.get("msg") or "неверное значение"
        where = f"{loc}: " if loc else ""
        raise ValueError(f"{where}{msg}") from exc
    return copy.deepcopy(loaded)


def parse_model_backend_text(text: str) -> dict[str, Any] | None:
    """Текст поля ``backend`` модели → оверлей транспорта или «снять секцию».

    Пустое поле — ``None``: секции нет, запрос берёт общий ``backend``.
    Явный ``{}`` — пустой оверлей. Записанный ключ потом переписывает одноимённый
    дефолт; сюда не входят сэмплинг и поля, которые рантайм читает только из
    общего ``backend``.
    """
    raw = text.strip()
    if not raw:
        return None
    loaded = _parse_mapping_text(raw)
    if loaded is None:
        return {}
    _reject_disallowed_backend_keys(loaded)
    return _validated_backend_overlay(loaded)


def effective_completion_kwargs(
    resolved: Any, settings: Any, *, section: str
) -> dict[str, Any]:
    """Kwargs секции резолвленного алиаса.

    Заполненный mapping — целиком, без слияния с глобальным словарём.
    Явный ``{}`` — пустой набор: в запрос эти ключи не кладутся, их выбирает
    сервер модели. Секции нет (``None``) — глобальный словарь своей роли:
    ``main`` читает ``backend.chat_completion_kwargs``, ``auxiliary`` —
    ``chat_completions.auxiliary_llm.completion_kwargs``. Друг в друга секции
    не подставляются. ``models.<alias>.backend.chat_completion_kwargs`` не
    читается.
    """
    chosen = getattr(resolved, section, None) if resolved is not None else None
    if isinstance(chosen, dict):
        return copy.deepcopy(chosen)
    if settings is None:
        return {}
    from gc_core.settings import settings_of

    if section == "main":
        global_kw = settings_of(settings).backend.chat_completion_kwargs
    else:
        global_kw = auxiliary_llm_from_config_dict(settings).completion_kwargs
    if isinstance(global_kw, dict):
        return copy.deepcopy(global_kw)
    return {}


def validate_model_alias(alias: str) -> None:
    """Алиас models.<alias> не может совпадать с зарезервированным словом уровня."""
    if alias in RESERVED_WORDS:
        raise ValueError(
            f"models: имя '{alias}' зарезервировано ({', '.join(RESERVED_WORDS)})"
        )


def validate_level_value(value: str) -> str:
    """level ∉ LEVELS — ошибка; `auto` — режим автовыбора (перевод в уровень —
    `auto_routing.levels.effective_target`)."""
    if value not in LEVEL_VALUES:
        raise ValueError(
            f"level должен быть одним из {LEVELS} или 'auto', получено '{value}'"
        )
    return value


def connection_parts(
    *,
    url: Any,
    token: Any,
    completions_url: Any,
    authorization: Any,
) -> tuple[bool, bool]:
    """§2, ЕДИНОЕ определение «адрес задан» / «токен задан» для всех точек проверки.

    Публичное: кроме обеих точек валидации им считает пару форма записи реестра
    (`tui/editors/model_entry_form`) — интерфейс обязан не давать собрать то, что
    отвергнет валидатор, а своё определение в форме было бы третьим.

    Адрес — по truthiness, потому что так его резолвит рантайм: or-цепочка
    `url → backend.completions_url → общий completions_url`
    (`model_router._resolve_alias`). Значит `url: ""` — это «своего адреса нет, пара
    наследуется», и правило обязано читать его так же, иначе отвергает законную форму,
    а `{url: "", token: ...}` пропускает как целую пару.

    `backend.proxy` в пару не входит: это строка транспорта эндпоинта. Она
    применяется сама по себе и не требует своего токена или адреса.

    Токен — по «значение задано» (`is not None`), потому что так его читает
    `ModelEntry.authorization_header`: `None` — наследовать, `""` — ЗАДАННЫЙ токен
    «сервер не требует авторизации»."""
    return (
        bool(url or completions_url),
        token is not None or authorization is not None,
    )


def resolve_entry_address(
    *, url: Any, completions_url: Any, inherited: Any = ""
) -> str:
    """ДЕЙСТВУЮЩИЙ адрес записи реестра — одна or-цепочка на всех, кто её произносит:
    свой `url` → своё второе написание `backend.completions_url` → унаследованный из
    эффективной секции `backend`. Тем же порядком резолвит рантайм
    (`model_router._resolve_alias`); разъехавшись, витрина назвала бы не тот хост, на
    который уйдёт запрос.

    Живёт рядом с `connection_parts` и по той же причине: спрашивают цепочку и витрина
    реестра (`tui/editors/models_section`), и форма записи (`tui/editors/
    model_entry_form`), а импортом друг у друга им не обойтись — секция сама тянет
    форму. Роутер сюда не ходит: у него свой слой снимка.

    `inherited=""` — вопрос «что задаёт САМА запись»: пусто значит «своего адреса нет,
    работает общий»."""
    return url or completions_url or inherited or ""


def _pair_violation(
    label: str, has_address: bool, has_token: bool
) -> str | None:
    """Текст отказа правила пары (§3) или None, если пара целая. Один на обе точки —
    иначе формулировки разъезжаются так же, как разъехались бы определения."""
    if has_address and not has_token:
        return (
            f"{label}: задан свой адрес, но не задан токен — укажите token "
            '(или token: "" — «сервер не требует авторизации»), либо уберите адрес, '
            "чтобы запись работала через общее подключение"
        )
    if has_token and not has_address:
        return (
            f"{label}: задан свой токен, но не задан адрес — укажите url, "
            "либо уберите token, чтобы запись работала через общее подключение"
        )
    return None


def _unresolved_pair_refs(entry) -> list[str]:
    """Имена переменных, из-за которых поля СВЯЗИ записи оказались пустыми.

    Смотрит ДОРЕЗОЛВНОЕ значение: после подстановки от `${ENV:X}` не остаётся ничего,
    кроме пустой строки, и назвать причину уже нечем."""
    from gc_core.config_raw import missing_env_refs

    if not isinstance(entry, dict):
        return []
    backend = (
        entry.get("backend") if isinstance(entry.get("backend"), dict) else {}
    )
    headers = (
        backend.get("chat_completion_headers")
        if isinstance(backend.get("chat_completion_headers"), dict)
        else {}
    )
    names: list[str] = []
    for value in (
        entry.get("url"),
        entry.get("token"),
        backend.get("completions_url"),
        headers.get("Authorization"),
    ):
        for name in missing_env_refs(value):
            if name not in names:
                names.append(name)
    return names


def validate_registry_pairs(
    raw: dict, source: str = "", *, raw_source=None
) -> None:
    """§3, точка B: проверка правила пары на СЫРОМ документе одного слоя, до слияния.

    Нужна отдельно от валидатора `ModelEntry`: слои сливаются `deep_merge` рекурсивно по
    ключам, поэтому слой, задавший ОДИН адрес поверх чужой полной пары, даёт итоговую
    запись с полной парой — и проверка итогового документа молчит, пока токен базы уходит
    на адрес слоя. Здесь же видно, ЧЕЙ это файл.

    Работает по сырому словарю потому, что другой формы у слоя до слияния нет: схема
    применяется один раз, к итоговому документу. Определение пары — общее с точкой A
    (`connection_parts`): pre-write валидаторы гоняют только A, и разъехавшиеся
    определения дали бы записанный из Settings файл, который следующий старт отвергнет
    здесь.

    `raw_source` — ТОТ ЖЕ документ до подстановки `${ENV:…}`. Правило судит значения
    ПОСЛЕ неё (решение владельца 2026-08-26, вариант Б), и незаданная переменная
    честно ломает пару: конфиг без неё работать не может. Но винить за это конфиг
    нельзя — человек забыл `export`, а не ошибся в файле, — поэтому причину называем
    по дорезолвному виду. Без `raw_source` поведение прежнее: имя переменной в
    подставленном значении не сохраняется, и выдумывать его нечестно."""
    models = (raw or {}).get("models")
    if not isinstance(models, dict):
        return
    prefix = f"{source}: " if source else ""
    for alias, entry in models.items():
        if not isinstance(entry, dict):
            continue
        backend = (
            entry.get("backend")
            if isinstance(entry.get("backend"), dict)
            else {}
        )
        headers = (
            backend.get("chat_completion_headers")
            if isinstance(backend.get("chat_completion_headers"), dict)
            else {}
        )
        problem = _pair_violation(
            f"{prefix}models.{alias}",
            *connection_parts(
                url=entry.get("url"),
                token=entry.get("token"),
                completions_url=backend.get("completions_url"),
                authorization=headers.get("Authorization"),
            ),
        )
        if problem:
            names = _unresolved_pair_refs(
                ((raw_source or {}).get("models") or {}).get(alias)
            )
            if names:
                raise ValueError(
                    f"{prefix}models.{alias}: не задана переменная окружения "
                    f"{', '.join(names)} — ссылка ${{ENV:…}} подставилась пустой, и "
                    "запись осталась без половины пары «адрес + токен». Экспортируйте "
                    "переменную либо впишите значение в конфиг прямо"
                )
            raise ValueError(problem)


def model_authorization_header(token: str | None) -> str | None:
    """Сохранить настроенную форму token для первой транспортной попытки."""
    return token


class ModelEntry(BaseModel):
    """Запись реестра `models`: алиас (ключ словаря) → провайдерская модель + точечные
    оверлеи backend/pricing поверх эффективной секции backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = None
    url: str | None = None
    token: SecretText | None = Field(
        default=None, json_schema_extra=gc_meta(secret=True)
    )
    pricing: ModelTariff | None = None
    backend: ModelBackendConfig | None = None
    # None — секция не задана: свой глобальный словарь (main —
    # chat_completion_kwargs, auxiliary — auxiliary_llm.completion_kwargs).
    # {} — задана и пуста: в запрос эти ключи не класть.
    main: dict[str, Any] | None = None
    auxiliary: dict[str, Any] | None = None
    # None — ключа нет, ход берёт глобальный strip_tags_projection.strip_think.
    # true — вырезать мысль из истории. false — копировать скрытое поле.
    strip_think: bool | None = None
    # Mid-turn пороги сжатия на алиасе. 0 / нет ключа — шкала не задана.
    # Хотя бы одно > 0 — режим модели (см. summarize.config.resolve_summary_limits).
    summary_trigger_chars: int = 0
    summary_trigger_tokens: int = 0

    @field_validator("strip_think", mode="before")
    @classmethod
    def _strip_think_flag(cls, value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        raise ValueError(
            "strip_think: true, false или отсутствие ключа (глобальный флаг)"
        )

    @field_validator(
        "summary_trigger_chars", "summary_trigger_tokens", mode="before"
    )
    @classmethod
    def _summary_trigger_null_is_unset(cls, value: Any) -> Any:
        # YAML null — «не задан», как у соседних ключей записи.
        return 0 if value is None else value

    @field_validator("summary_trigger_chars", "summary_trigger_tokens")
    @classmethod
    def _summary_trigger_nonneg(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                "summary_trigger_chars/tokens: отрицательное недопустимо"
            )
        return value

    @field_validator("main", "auxiliary", mode="before")
    @classmethod
    def _kwargs_section(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        raise ValueError("секция kwargs должна быть mapping")

    @model_validator(mode="after")
    def _validate_no_duplicate_settings(self) -> ModelEntry:
        if (
            self.token is not None
            and self.backend is not None
            and self.backend.chat_completion_headers.get("Authorization")
            is not None
        ):
            raise ValueError(
                "models.<alias>: token и backend.chat_completion_headers.Authorization "
                "заданы одновременно — оставьте что-то одно"
            )
        if (
            self.url is not None
            and self.backend is not None
            and self.backend.completions_url
        ):
            raise ValueError(
                "models.<alias>: url и backend.completions_url — два написания одного и того же — оставьте что-то одно"
            )
        return self

    def _connection_parts(self) -> tuple[bool, bool]:
        """(адрес задан, токен задан) для этой записи — ядровым определением §2.

        Одна адаптация формы `ModelEntry` на всех спрашивающих: и правило пары, и
        предупреждение об ослабленном транспорте (§7) судят по одному ответу — второе
        прочтение тех же ключей разъехалось бы на первой же правке определения."""
        backend = self.backend
        return connection_parts(
            url=self.url,
            token=self.token,
            completions_url=backend.completions_url
            if backend is not None
            else None,
            authorization=(
                backend.chat_completion_headers.get("Authorization")
                if backend is not None
                else None
            ),
        )

    @model_validator(mode="after")
    def _validate_connection_pair(self) -> ModelEntry:
        """§2: своё подключение задаётся ЦЕЛИКОМ. Адрес и токен принадлежат одному
        провайдеру: половина пары либо уносит общий ключ на чужой хост (свой адрес без
        токена), либо уезжает вслед за сменой общего адреса (свой токен без адреса).

        `backend.proxy` адрес не задаёт: строка применяется к запросу этого
        эндпоинта и без своего токена. `use_ssl`/сертификаты — тоже не адрес, у них
        своё предупреждение (спека §7). Что считать заданным — `connection_parts`, общее
        с проверкой слоя."""
        problem = _pair_violation("models.<alias>", *self._connection_parts())
        if problem:
            raise ValueError(problem)
        return self

    def weakens_shared_transport(self) -> bool:
        """§7: запись с УНАСЛЕДОВАННОЙ парой меняет транспорт под собой.

        `use_ssl`/`cert_path`/`key_path` в правило пары не входят (решение владельца 2):
        они не меняют адрес, а ослабляют защиту запроса — поэтому notice, а не отказ.
        Ослабляется при этом запрос, который уходит с ОБЩИМ токеном, и сказать об этом
        человеку обязаны: сам он в записи видит только сетевой квирк.

        Своя пара сюда не попадает: там по изменённому каналу уходит свой ключ записи
        на свой адрес — про общий токен предупреждать не о чем. Текст предупреждения —
        у того, кто собирает стартовые notices секций моделей."""
        backend = self.backend
        if backend is None or any(self._connection_parts()):
            return False
        return (
            not backend.use_ssl
            or bool(backend.cert_path)
            or bool(backend.key_path)
        )

    @property
    def authorization_header(self) -> str | None:
        """Token записи как настроен: None → наследование, "" → явно без
        авторизации. Если сервер ответил 401 и принял другую форму (с
        ``Bearer`` или без), транспорт до конца процесса начинает с неё."""
        return model_authorization_header(secret_value(self.token))


class SlotsConfig(BaseModel):
    """Уровни light/default/max — указатели на алиас из `models`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    light: str | None = None
    default: str | None = None
    max: str | None = None


class AssignConfig(BaseModel):
    """Привязки потребителей → уровень или алиас. Ключ `main` запрещён — основной
    трейс управляется уровнем (`level`/`/level`), не привязкой."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finish_gate: str | None = None
    text_answer_judge: str | None = None
    command_judge: str | None = None
    summary: str | None = None
    intent_scope: str | None = None
    memory_compaction: str | None = None
    vision: str | None = None
    routing_judge: str | None = None
    subagents: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _forbid_main(cls, data: Any) -> Any:
        if isinstance(data, dict) and "main" in data:
            raise ValueError(
                "assign.main запрещён — основной трейс управляется уровнем, не привязкой"
            )
        return data
