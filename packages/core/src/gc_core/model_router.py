"""ModelRouter — единственный владелец вопроса «какой моделью идёт этот вызов».

Router читает секции `models`/`slots`/`assign` из снимка настроек (``settings_of``:
``AppConfig``, ``LoadedSettings`` или контекст), зафиксированного
при сборке executor, и активный уровень — через
инжектируемый `level_provider`: сессионный слой живёт вне Router (`/level` меняет его
без пересборки executor).

Предохранитель — сессионный набор алиасов, признанных недоступными. Трипает его любой
потребитель, у которого подтверждённая недоступность пережила все существующие ретраи
(`processing_responses`, `utils/llm_chat_client`, `SubagentRunner`); Router держит на
нём единую точку отказа — открытый алиас роняет `resolve()` мгновенно
(`AliasCircuitOpenError`), без похода в транспорт и во ВСЕХ потребителях этой сессии.
Выбор фоллбэк-цели (активная модель / честная ошибка / рестарт субагента) — задача
тех же потребителей, не Router'а.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from gc_core.config_modules.model_registry import (
    LEVEL_FALLBACK,
    LEVELS,
    ModelEntry,
)
from gc_core.settings import settings_of

# Канонический реестр потребителей (спека §5.1). `subagent:<agent_name>` — отдельный
# префиксный consumer id, не перечислимый статически здесь (канонический реестр имён
# субагентов живёт в config_modules/model_sections_validation.py, схема уже проверяет
# ссылочную целостность на этапе валидации конфига).
CONSUMERS = (
    "main",
    "finish_gate",
    "text_answer_judge",
    "command_judge",
    "summary",
    "intent_scope",
    "memory_compaction",
    "auto_judge",
    "vision",
)
SUBAGENT_CONSUMER_PREFIX = "subagent:"

# Потребители, которые без привязки работают фиксированным уровнем, а не
# активным уровнем хода. Судьи — как классификатор у эталона: проверка не
# должна дорожать и медлить оттого, что человек перевёл ход на max.
_UNBOUND_LEVELS = {"auto_judge": "default", "command_judge": "default"}


def unbound_level(consumer: str) -> str | None:
    """Уровень потребителя без привязки; None — идёт за активным уровнем.
    Одно место правды и для резолва, и для экрана настроек."""
    return _UNBOUND_LEVELS.get(consumer)


# Привязки assign, у которых значение читается скалярным полем AssignConfig (все,
# кроме subagents-словаря и запрещённого main).
_SCALAR_ASSIGN_FIELDS = (
    "finish_gate",
    "text_answer_judge",
    "command_judge",
    "summary",
    "intent_scope",
    "memory_compaction",
    "auto_judge",
    "vision",
)


@dataclass(frozen=True)
class ResolvedModel:
    """Комплект «модель + куда слать + чем авторизоваться», отданный потребителю."""

    alias: str | None  # None в легаси
    model: str  # провайдерский id
    url: str
    authorization: (
        str | None
    )  # None=наследовать эффективный заголовок, ""=явно без, иначе значение
    backend_overrides: dict  # плоские ключи backend поверх эффективных
    tariff: object | None
    source: str  # человекочитаемое «почему» — питает витрины/футер/notices
    # None — секция в записи не задана. {} — задана пустой (replace без ключей).
    main: dict | None = None
    auxiliary: dict | None = None
    # None — у алиаса ключа нет, persist берёт глобальный strip_think.
    strip_think: bool | None = None
    # Mid-turn пороги сжатия. 0 = не задан (как отсутствие ключа в снимке).
    summary_trigger_chars: int = 0
    summary_trigger_tokens: int = 0


class AliasCircuitOpenError(RuntimeError):
    """Алиас уже открыт в предохранителе (спека §8): резолв отказывает мгновенно,
    без обращения к транспорту — во ВСЕХ потребителях этой сессии. ``resolved``
    несёт полный комплект (модель/url/source), собранный ДО проверки breaker'а,
    чтобы вызывающий код мог построить честное сообщение об ошибке без похода
    в реестр повторно."""

    def __init__(self, resolved: ResolvedModel, reason: str) -> None:
        self.resolved = resolved
        self.alias = resolved.alias
        self.reason = reason
        super().__init__(
            f"алиас '{resolved.alias}' в предохранителе: {reason}"
        )


class CircuitBreaker:
    """Сессионный per-alias предохранитель (спека §8). Ключ — алиас модели, не привязка.

    Владелец состояния — сессия-тред: `snapshot()`/`restore()` персистят набор открытых
    алиасов в meta треда, чтобы предохранитель пережил пересборку executor внутри сессии.
    """

    def __init__(self) -> None:
        self._tripped: dict[str, str] = {}

    def is_open(self, alias: str) -> bool:
        return alias in self._tripped

    def trip(self, alias: str, reason: str) -> None:
        self._tripped[alias] = reason

    def snapshot(self) -> dict[str, str]:
        return dict(self._tripped)

    @classmethod
    def restore(cls, snapshot: dict) -> CircuitBreaker:
        breaker = cls()
        breaker._tripped = dict(snapshot or {})
        return breaker

    def replace_from(self, snapshot: dict) -> None:
        """Restore-in-place: заменить содержимое предохранителя набором из снапшота
        БЕЗ подмены объекта. Прогретый executor и витрины держат ссылку на ЭТОТ
        объект (передан в `ctx.model_router` при сборке executor); подмена ссылки
        осиротила бы их — трипы рана уходили бы в старый breaker, а persist писал бы
        новый пустой (спека §8). См. `TuiApp.load_circuit_breaker`."""
        self._tripped = dict(snapshot or {})

    def reset(self) -> None:
        self._tripped.clear()


def _level_phrase(requested_level: str, resolved_level: str) -> str:
    if requested_level == resolved_level:
        return f"уровень {requested_level}"
    return f"фоллбэк {requested_level} → {resolved_level}"


def _nonneg_int(raw: Any) -> int:
    """0 и отсутствие ключа — одно и то же для порогов сжатия на модели."""
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def serialize_model_entry(entry: ModelEntry) -> dict[str, Any]:
    """Проекция ``ModelEntry`` для роутера.

    ``authorization_header`` — token записи в настроенной форме (None=наследовать,
    ""=явно без, иначе значение; форму с ``Bearer`` или без выбирает проба). ``backend_overrides`` — только явно заданные ключи
    (``exclude_unset``), чтобы Router накладывал поверх эффективного backend лишь то,
    что модель реально переопределяет, а не дефолты ``BackendConfig``.
    """
    projected = {
        "model": entry.model,
        "url": entry.url,
        "authorization_header": entry.authorization_header,
        "pricing": entry.pricing.model_dump()
        if entry.pricing is not None
        else None,
        "backend_overrides": (
            entry.backend.model_dump(exclude_unset=True)
            if entry.backend is not None
            else {}
        ),
    }
    # Пустой dict тоже пишем: он заменяет глобальные kwargs, а отсутствие ключа —
    # наследование. None (секция не задана) в снимок не кладём.
    if isinstance(entry.main, dict):
        projected["main"] = deepcopy(entry.main)
    if isinstance(entry.auxiliary, dict):
        projected["auxiliary"] = deepcopy(entry.auxiliary)
    if isinstance(entry.strip_think, bool):
        projected["strip_think"] = entry.strip_think
    # 0 = «не задан» — резолв порога читает 0 и отсутствие ключа одинаково.
    if entry.summary_trigger_chars > 0:
        projected["summary_trigger_chars"] = entry.summary_trigger_chars
    if entry.summary_trigger_tokens > 0:
        projected["summary_trigger_tokens"] = entry.summary_trigger_tokens
    return projected


class ModelRouter:
    """Резолвит `consumer -> ResolvedModel` из снимка конфига (спека §6.3)."""

    def __init__(
        self,
        settings: Any,
        level_provider: Callable[[], str],
        breaker: CircuitBreaker | None = None,
        force_legacy: bool = False,
        unbound_override: ResolvedModel | None = None,
    ) -> None:
        """``unbound_override`` — явный канал «во что резолвится этот Router, если
        потребитель НЕ имеет assign-привязки» (используется дочерним Router'ом
        субагента, спека §7 п.4). Консультируется ТОЛЬКО когда `_lookup_binding`
        вернул None — явные привязки (и уровнем, и алиасом, §5.2: индирекция
        через слоты/реестр) резолвятся как обычно, override их не перекрывает.
        `None` (по умолчанию) — прежнее поведение (level_provider → слот).

        ``settings`` — снимок настроек: всё, что принимает ``settings_of``
        (``AppConfig``, ``LoadedSettings``, контекст); ``None`` — умолчания."""

        # Снимок фиксируется типизированным: клон (`with_override`) берёт его же.
        self._settings = settings_of(settings)
        self._level_provider = level_provider
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self._force_legacy = force_legacy
        self._unbound_override = unbound_override
        # Записи реестра — в проекции `serialize_model_entry`: вычисленный
        # `authorization_header`, `backend_overrides` только из явных ключей,
        # тариф словарём (его и ждут потребители `ResolvedModel.tariff`).
        self._models: dict[str, Any] = {
            alias: serialize_model_entry(entry)
            for alias, entry in self._settings.models.items()
        }
        self._slots: dict[str, Any] = self._settings.slots.model_dump()
        self._assign: dict[str, Any] = self._settings.assign.model_dump()

    def registry_empty(self) -> bool:
        return not self._models

    def active_level(self) -> str:
        return self._level_provider()

    @property
    def force_legacy(self) -> bool:
        """Легаси-форс этого Router'а (спека §2: CLI `--model`/`--completions_url`
        поверх непустого реестра). Дочерний Router субагента (`_build_child_model_router`)
        обязан унаследовать этот флаг — форс отключает резолв уровней/алиасов ВЕЗДЕ,
        а не только у родительского `main`."""
        return self._force_legacy

    @property
    def is_subagent_child(self) -> bool:
        """True — этот Router построен для субагента (``unbound_override`` задан,
        спека §7 п.4): ``SubagentRunner._build_child_model_router`` ставит override
        для ЛЮБОГО субагента, пинованного или нет (непинованный резолвится через
        override в ту же активную модель, что вернул бы обычный резолв уровня) —
        не путать с «у этого субагента ЕСТЬ пин отдельно от активной модели».
        Читают потребители фоллбэков (спека §8): main-трейс (`processing_responses`)
        пропускает class-2-обработку для ЛЮБОГО ребёнка-субагента (её ведёт
        `SubagentRunner._maybe_pin_fallback` — пин там или нет, решает сравнение
        резолвов, а не этот флаг)."""
        return self._unbound_override is not None

    def with_override(self, override: ResolvedModel | None) -> ModelRouter:
        """Клон Router'а с другим ``unbound_override`` — тот же снимок конфига,
        level_provider и breaker (спека §8: деградация субагента на активную
        модель без пересборки снимка/уровня, см. ``SubagentRunner``)."""
        return ModelRouter(
            self._settings,
            level_provider=self._level_provider,
            breaker=self.breaker,
            force_legacy=self._force_legacy,
            unbound_override=override,
        )

    def resolve(
        self, consumer: str, *, check_breaker: bool = True
    ) -> ResolvedModel:
        """``check_breaker=False`` — витринный резолв (футер, `/status`, спека §12.2):
        открытый алиас не роняет резолв, модель отдаётся честно. Рантайм-потребители
        оставляют дефолт ``True`` — открытый алиас отказывает мгновенно (§8)."""
        if not (
            consumer in CONSUMERS
            or consumer.startswith(SUBAGENT_CONSUMER_PREFIX)
        ):
            raise ValueError(
                f"неизвестный consumer '{consumer}' — используйте ModelRouter.CONSUMERS "
                f"или '{SUBAGENT_CONSUMER_PREFIX}<agent_name>'"
            )

        if self._force_legacy:
            return self._resolve_legacy(source="легаси-форс --model")
        if self.registry_empty():
            return self._resolve_legacy(source="легаси")

        binding = self._lookup_binding(consumer)
        if binding is not None:
            label, value = binding
            if value in LEVELS:
                return self._resolve_level(
                    value,
                    source_prefix=f"привязка {label}→",
                    check_breaker=check_breaker,
                )
            return self._resolve_alias(
                value,
                source=f"привязка {label}→{value}",
                check_breaker=check_breaker,
            )

        if self._unbound_override is not None:
            return self._unbound_override

        level = unbound_level(consumer)
        if level is not None:
            return self._resolve_level(
                level,
                source_prefix=f"{consumer} без привязки → ",
                check_breaker=check_breaker,
            )

        level = self._level_provider()
        return self._resolve_level(
            level, source_prefix="активный ", check_breaker=check_breaker
        )

    def resolve_main_at_level(
        self, level: str | None, *, check_breaker: bool = True
    ) -> ResolvedModel:
        """Модель main-трейса для УРОВНЯ ВЫЗОВА — единый ответ для запроса и для
        всего, что обязано принадлежать той же модели (пороги сжатия истории).

        Уровень задаёт вызывающий: сегодня это уровень, зафиксированный на ход
        (`turn_boundary.begin_turn` → `state.turn_level`, спека §6.4) — живой
        `/level` меняется без пересборки executor, и резолв по нему увёл бы модель
        посреди цикла tool-call'ов. Уровня нет (ход не начинали через `begin_turn`
        — легаси/тестовые двойники) или Router дочерний (у субагента модель уже
        заморожена в самом Router'е) — обычный резолв `main`."""
        if not level or self.is_subagent_child:
            return self.resolve("main", check_breaker=check_breaker)
        return self.resolve_level(level, check_breaker=check_breaker)

    def resolve_level(
        self, level: str, *, check_breaker: bool = True
    ) -> ResolvedModel:
        """Резолв ПРОИЗВОЛЬНОГО уровня, не только активного (спека §12.1) — витринам
        (палитра `/level`) нужно честно показать, во что резолвится КАЖДАЯ из трёх
        позиций трека, а не только та, что сейчас активна через `level_provider`.
        Та же ветка легаси/легаси-форс, что у `resolve()`; фоллбэк-цепочка уровня —
        как обычно (§4 «Матрица фоллбэков»).

        Не только витринный: этим же путём резолвится main-трейс по уровню,
        зафиксированному на ход (`resolve_main_at_level`, спека §6.4) — поэтому
        предохранитель здесь проверяется как в рантайме (`check_breaker` по
        умолчанию), а не отключается «раз это витрина». ``check_breaker=False`` —
        чтение свойств записи (пороги сжатия): открытый алиас его не роняет."""
        if self._force_legacy:
            return self._resolve_legacy(source="легаси-форс --model")
        if self.registry_empty():
            return self._resolve_legacy(source="легаси")
        return self._resolve_level(
            level, source_prefix="", check_breaker=check_breaker
        )

    def _lookup_binding(self, consumer: str) -> tuple[str, str] | None:
        if consumer.startswith(SUBAGENT_CONSUMER_PREFIX):
            name = consumer[len(SUBAGENT_CONSUMER_PREFIX) :]
            subagents = self._assign.get("subagents") or {}
            if subagents.get(name) is not None:
                return name, subagents[name]
            if subagents.get("*") is not None:
                return name, subagents["*"]
            return None
        if consumer == "main":
            return None
        value = self._assign.get(consumer)
        if consumer in _SCALAR_ASSIGN_FIELDS and value is not None:
            return consumer, value
        return None

    def _resolve_level(
        self, level: str, source_prefix: str, *, check_breaker: bool = True
    ) -> ResolvedModel:
        for candidate_level in LEVEL_FALLBACK[level]:
            alias = self._slots.get(candidate_level)
            if alias:
                phrase = _level_phrase(level, candidate_level)
                return self._resolve_alias(
                    alias,
                    source=f"{source_prefix}{phrase}",
                    check_breaker=check_breaker,
                )
        if len(self._models) == 1:
            (only_alias,) = self._models.keys()
            phrase = f"уровень {level} (единственная модель реестра)"
            return self._resolve_alias(
                only_alias,
                source=f"{source_prefix}{phrase}",
                check_breaker=check_breaker,
            )
        raise ValueError(
            f"не удалось резолвить уровень '{level}': ни один слот не задан, моделей в реестре не ровно одна"
        )

    def _resolve_alias(
        self, alias: str, source: str, *, check_breaker: bool = True
    ) -> ResolvedModel:
        entry = self._models.get(alias)
        if entry is None:
            raise ValueError(
                f"алиас '{alias}' отсутствует в merged-реестре models"
            )
        backend_overrides = entry.get("backend_overrides") or {}
        model_id = entry.get("model") or alias
        main = (
            entry.get("main") if isinstance(entry.get("main"), dict) else None
        )
        auxiliary = (
            entry.get("auxiliary")
            if isinstance(entry.get("auxiliary"), dict)
            else None
        )
        strip_think = entry.get("strip_think")
        if not isinstance(strip_think, bool):
            strip_think = None
        summary_trigger_chars = _nonneg_int(entry.get("summary_trigger_chars"))
        summary_trigger_tokens = _nonneg_int(
            entry.get("summary_trigger_tokens")
        )
        url = (
            entry.get("url")
            or backend_overrides.get("completions_url")
            or self._settings.backend.completions_url
        )
        resolved = ResolvedModel(
            alias=alias,
            model=model_id,
            url=url,
            authorization=entry.get("authorization_header"),
            backend_overrides=backend_overrides,
            tariff=entry.get("pricing"),
            source=source,
            main=main,
            auxiliary=auxiliary,
            strip_think=strip_think,
            summary_trigger_chars=summary_trigger_chars,
            summary_trigger_tokens=summary_trigger_tokens,
        )
        # Сессионный предохранитель (спека §8): открытый алиас — мгновенный отказ
        # резолва, без обращения к транспорту, В ЛЮБОМ потребителе (единая точка).
        # ``check_breaker=False`` — витринный резолв (футер/`/status`): показать
        # модель честно, не роняя резолв (§12.2).
        if check_breaker and self.breaker.is_open(alias):
            raise AliasCircuitOpenError(
                resolved, self.breaker.snapshot().get(alias, "")
            )
        return resolved

    def _resolve_legacy(self, source: str) -> ResolvedModel:
        return ResolvedModel(
            alias=None,
            model=self._settings.agent.model,
            url=self._settings.backend.completions_url,
            authorization=None,
            backend_overrides={},
            tariff=None,
            source=source,
        )
