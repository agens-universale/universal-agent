"""``UsageAccount`` — аккумулятор токенов одного агента по категориям.

Хранится на ``State`` (``state.usage``); агрегируется вверх через ``merge``
(вклад субагентов сворачивается в категорию ``subagent`` у родителя).
Персистится в ``meta.json`` (``to_meta``/``from_meta``).

Второе измерение — модель вызова (``by_model``): под автовыбором один ход идёт на
нескольких моделях, и расход каждой нужен отдельно. Внутри каждой категории
``by_model`` хранит также биллённые токены для тарифного расчёта остатка.
Фактические деньги ведутся по категориям.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Категории вызовов LLM.
CATEGORY_MAIN = "main"
CATEGORY_SUMMARY = "summary"
CATEGORY_FINISH_GATE = "finish_gate"
CATEGORY_COMMAND_MODERATION = "command_moderation"
CATEGORY_PROJECT_MEMORY = "project_memory"
CATEGORY_INTENT_SCOPE = "intent_scope"
CATEGORY_STRUCTURED_OUTPUT = "structured_output"
CATEGORY_SUBAGENT = "subagent"
CATEGORY_VISION = "vision"
CATEGORY_ROUTING_JUDGE = "routing_judge"
CATEGORY_UNCATEGORIZED = "uncategorized"

_KNOWN_CATEGORIES = (
    CATEGORY_MAIN,
    CATEGORY_SUMMARY,
    CATEGORY_FINISH_GATE,
    CATEGORY_COMMAND_MODERATION,
    CATEGORY_PROJECT_MEMORY,
    CATEGORY_INTENT_SCOPE,
    CATEGORY_STRUCTURED_OUTPUT,
    CATEGORY_SUBAGENT,
    CATEGORY_VISION,
    CATEGORY_ROUTING_JUDGE,
    CATEGORY_UNCATEGORIZED,
)

# ``cached`` — часть ``input``, попавшая в кэш-ставку (не отдельные токены,
# поэтому в total не добавляется). Биллится дешевле при наличии cache_read.
# ``billed_*`` — подмножество вызовов, для которых известна фактическая цена;
# токены не дублируются: тарифный остаток = input − billed_input и т.д.
_FIELDS = (
    "input",
    "output",
    "cached",
    "calls",
    "missing_usage_calls",
    "billed_calls",
    "billed_input",
    "billed_output",
    "billed_cached",
)
# Деньги ведутся отдельным словарём «валюта → сумма»: складывать суммы разных
# валют нельзя, а валюта — свойство конкретного вызова (её знает провайдер
# в момент ответа), поэтому она фиксируется при записи, а не при показе.
MONEY_FIELD = "cost_by_currency"
# Поля бакета модели: токены и вызовы, без денег (см. докстринг модуля).
_MODEL_FIELDS = ("input", "output", "cached", "calls", "missing_usage_calls")

# Ключ для сумм, записанных версией без валюты (старые meta): «валюта неизвестна».
# Разрешается при показе — по адресу эндпоинта из текущего конфига.
LEGACY_CURRENCY = ""

# Ниже этого порога денежная величина считается нулём (float-хвосты вычитания).
COST_EPSILON = 1e-9


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _money_from_raw(bucket: Dict[str, Any]) -> Dict[str, float]:
    """Деньги бакета из сырого meta: новый словарь либо legacy-число."""
    raw = bucket.get(MONEY_FIELD)
    if isinstance(raw, dict):
        return {str(cur): _num(amount) for cur, amount in raw.items()}
    legacy = bucket.get("cost")
    if isinstance(legacy, (int, float)) and legacy:
        return {LEGACY_CURRENCY: float(legacy)}
    return {}


def category_for_request_label(request_label: str | None) -> str:
    """Маппинг ``request_label`` вспомогательного вызова → категория."""
    label = (request_label or "").strip().lower()
    if not label:
        return CATEGORY_MAIN
    if label.startswith("subagent"):
        return CATEGORY_SUBAGENT
    if label.startswith("summary"):
        return CATEGORY_SUMMARY
    if "finish_gate" in label or "intent_guard" in label:
        return CATEGORY_FINISH_GATE
    if "command_moderation" in label or "command_judge" in label:
        return CATEGORY_COMMAND_MODERATION
    if "project_memory" in label:
        return CATEGORY_PROJECT_MEMORY
    if "intent_scope" in label:
        return CATEGORY_INTENT_SCOPE
    if label.startswith("vision"):
        return CATEGORY_VISION
    if label == "structured_output":
        return CATEGORY_STRUCTURED_OUTPUT
    if "routing_judge" in label:
        return CATEGORY_ROUTING_JUDGE
    return CATEGORY_MAIN


def _empty_bucket() -> Dict[str, Any]:
    bucket: Dict[str, Any] = dict.fromkeys(_FIELDS, 0)
    bucket[MONEY_FIELD] = {}
    return bucket


class UsageAccount:
    def __init__(
        self,
        by_category: Optional[Dict[str, Dict[str, Any]]] = None,
        by_model: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.by_category: Dict[str, Dict[str, Any]] = {}
        self.by_model: Dict[str, Dict[str, int]] = {}
        if by_category:
            for category, bucket in by_category.items():
                if not isinstance(bucket, dict):
                    continue
                dst = self._bucket(str(category))
                for field in _FIELDS:
                    raw = bucket.get(field, 0)
                    dst[field] = (
                        int(raw) if isinstance(raw, (int, float)) else 0
                    )
                dst[MONEY_FIELD] = _money_from_raw(bucket)
                models = bucket.get("by_model")
                if isinstance(models, dict):
                    for model, model_bucket in models.items():
                        if not isinstance(model_bucket, dict):
                            continue
                        attributed = self._category_model_bucket(
                            str(category), str(model)
                        )
                        for field in _FIELDS:
                            attributed[field] = int(_num(model_bucket.get(field, 0)))
        if by_model:
            for model, bucket in by_model.items():
                if not isinstance(bucket, dict):
                    continue
                dst = self._model_bucket(str(model))
                for field in _MODEL_FIELDS:
                    raw = bucket.get(field, 0)
                    dst[field] = (
                        int(raw) if isinstance(raw, (int, float)) else 0
                    )

    def _bucket(self, category: str) -> Dict[str, Any]:
        bucket = self.by_category.get(category)
        if bucket is None:
            bucket = _empty_bucket()
            self.by_category[category] = bucket
        return bucket

    def _model_bucket(self, model: str) -> Dict[str, int]:
        bucket = self.by_model.get(model)
        if bucket is None:
            bucket = {field: 0 for field in _MODEL_FIELDS}
            self.by_model[model] = bucket
        return bucket

    def _category_model_bucket(
        self, category: str, model: str
    ) -> Dict[str, int]:
        models = self._bucket(category).setdefault("by_model", {})
        return models.setdefault(model, dict.fromkeys(_FIELDS, 0))

    def add(
        self,
        category: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        cost: Optional[float] = None,
        billed: bool = False,
        currency: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Учесть один вызов. ``currency`` — валюта фактической цены (её знает
        транспорт по адресу вызова); не передана → сумма ляжет как «валюта
        неизвестна» и будет разрешена при показе по текущему конфигу.
        ``model`` — модель вызова из тела запроса; не передана → в разбивку по
        моделям вызов не попадает."""
        bucket = self._bucket(category)
        inp = max(0, int(input_tokens or 0))
        out = max(0, int(output_tokens or 0))
        cached = min(max(0, int(cached_tokens or 0)), inp)
        buckets = [bucket]
        if model:
            buckets.append(self._category_model_bucket(category, model))
        for target in buckets:
            target["input"] += inp
            target["output"] += out
            target["cached"] += cached
            target["calls"] += 1
            if billed:
                target["billed_calls"] += 1
                target["billed_input"] += inp
                target["billed_output"] += out
                target["billed_cached"] += cached
        if model:
            model_bucket = self._model_bucket(model)
            model_bucket["input"] += inp
            model_bucket["output"] += out
            model_bucket["cached"] += cached
            model_bucket["calls"] += 1
        if billed:
            key = currency if currency is not None else LEGACY_CURRENCY
            money = bucket[MONEY_FIELD]
            money[key] = money.get(key, 0.0) + float(cost or 0.0)

    def add_missing(self, category: str, model: Optional[str] = None) -> None:
        """Вызов был, но провайдер не вернул usage: токены не считаем."""
        buckets = [self._bucket(category)]
        if model:
            buckets.append(self._model_bucket(model))
            buckets.append(self._category_model_bucket(category, model))
        for bucket in buckets:
            bucket["calls"] += 1
            bucket["missing_usage_calls"] += 1

    def merge(
        self, other: UsageAccount, as_category: Optional[str] = None
    ) -> None:
        """Влить чужой счёт. ``as_category`` — свернуть все категории other в одну
        (используется для субагентов: их вклад → категория ``subagent`` родителя).
        Модели не сворачиваются: токены ребёнка ушли в реальную модель."""
        if not isinstance(other, UsageAccount):
            return
        for category, bucket in other.by_category.items():
            dst = self._bucket(as_category or category)
            for field in _FIELDS:
                dst[field] += int(bucket.get(field, 0) or 0)
            dst_money = dst[MONEY_FIELD]
            for cur, amount in (bucket.get(MONEY_FIELD) or {}).items():
                dst_money[cur] = dst_money.get(cur, 0.0) + _num(amount)
            for model, model_bucket in (bucket.get("by_model") or {}).items():
                attributed = self._category_model_bucket(
                    as_category or category, model
                )
                for field in _FIELDS:
                    attributed[field] += int(model_bucket.get(field, 0) or 0)
        for model, bucket in other.by_model.items():
            dst = self._model_bucket(model)
            for field in _MODEL_FIELDS:
                dst[field] += int(bucket.get(field, 0) or 0)

    def delta_from(self, baseline: UsageAccount) -> UsageAccount:
        """Новый аккаунт = self − baseline (по полям, не ниже 0).

        Используется для usage именно текущего прогона при resume треда, где
        ``self`` — кумулятив, а ``baseline`` — снимок до старта прогона.
        """
        out = UsageAccount()
        base = (
            baseline.by_category if isinstance(baseline, UsageAccount) else {}
        )
        for category, bucket in self.by_category.items():
            base_bucket = base.get(category, {})
            dst = out._bucket(category)
            has_delta = False
            for field in _FIELDS:
                value = max(
                    0,
                    int(
                        _num(bucket.get(field, 0))
                        - _num(base_bucket.get(field, 0))
                    ),
                )
                dst[field] = value
                if value:
                    has_delta = True
            base_money = base_bucket.get(MONEY_FIELD) or {}
            dst_money = dst[MONEY_FIELD]
            for cur, amount in (bucket.get(MONEY_FIELD) or {}).items():
                # Эпсилон на КАЖДУЮ валюту: иначе float-хвост вычитания оставит
                # ключ с 1e-17 и создаст фантомную валюту в отчёте.
                value = max(0.0, _num(amount) - _num(base_money.get(cur, 0.0)))
                if value < COST_EPSILON:
                    continue
                dst_money[cur] = value
                has_delta = True
            base_attribution = base_bucket.get("by_model") or {}
            for model, model_bucket in (bucket.get("by_model") or {}).items():
                base_model = base_attribution.get(model, {})
                attributed = {
                    field: max(
                        0,
                        int(_num(model_bucket.get(field, 0)) - _num(base_model.get(field, 0))),
                    )
                    for field in _FIELDS
                }
                if any(attributed.values()):
                    dst.setdefault("by_model", {})[model] = attributed
            if not has_delta:
                del out.by_category[category]
        base_models = (
            baseline.by_model if isinstance(baseline, UsageAccount) else {}
        )
        for model, bucket in self.by_model.items():
            base_bucket = base_models.get(model, {})
            delta = {
                field: max(
                    0,
                    int(
                        _num(bucket.get(field, 0))
                        - _num(base_bucket.get(field, 0))
                    ),
                )
                for field in _MODEL_FIELDS
            }
            if any(delta.values()):
                out.by_model[model] = delta
        return out

    def totals(self) -> Dict[str, Any]:
        out: Dict[str, Any] = dict.fromkeys(_FIELDS, 0)
        money: Dict[str, float] = {}
        for bucket in self.by_category.values():
            for field in _FIELDS:
                out[field] += int(bucket.get(field, 0) or 0)
            for cur, amount in (bucket.get(MONEY_FIELD) or {}).items():
                money[cur] = money.get(cur, 0.0) + _num(amount)
        out[MONEY_FIELD] = money
        out["total"] = out["input"] + out["output"]
        return out

    def is_empty(self) -> bool:
        return self.totals()["calls"] == 0

    def to_meta(self) -> Dict[str, Any]:
        out: Dict[str, Dict[str, Any]] = {}
        for category, bucket in self.by_category.items():
            # Копия обязана быть глубокой: словарь валют мутируемый, иначе
            # накопитель продолжит менять уже сериализованный снимок.
            copy = dict(bucket)
            copy[MONEY_FIELD] = dict(bucket.get(MONEY_FIELD) or {})
            if bucket.get("by_model"):
                copy["by_model"] = {
                    model: dict(values)
                    for model, values in bucket["by_model"].items()
                }
            out[category] = copy
        meta: Dict[str, Any] = {"by_category": out}
        if self.by_model:
            meta["by_model"] = {
                model: dict(bucket) for model, bucket in self.by_model.items()
            }
        return meta

    @classmethod
    def from_meta(cls, raw: Any) -> UsageAccount:
        if not isinstance(raw, dict):
            return cls()
        by_category = raw.get("by_category")
        if not isinstance(by_category, dict):
            return cls()
        by_model = raw.get("by_model")
        return cls(
            by_category=by_category,
            by_model=by_model if isinstance(by_model, dict) else None,
        )

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"UsageAccount({self.totals()})"
