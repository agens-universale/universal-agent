"""``UsageAccount`` — аккумулятор токенов одного агента по категориям.

Хранится на ``State`` (``state.usage``); агрегируется вверх через ``merge``
(вклад субагентов сворачивается в категорию ``subagent`` у родителя).
Персистится в ``meta.json`` (``to_meta``/``from_meta``).
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
CATEGORY_SUBAGENT = "subagent"
CATEGORY_UNCATEGORIZED = "uncategorized"

_KNOWN_CATEGORIES = (
    CATEGORY_MAIN,
    CATEGORY_SUMMARY,
    CATEGORY_FINISH_GATE,
    CATEGORY_COMMAND_MODERATION,
    CATEGORY_PROJECT_MEMORY,
    CATEGORY_INTENT_SCOPE,
    CATEGORY_SUBAGENT,
    CATEGORY_UNCATEGORIZED,
)

# ``cached`` — часть ``input``, попавшая в кэш-ставку (не отдельные токены,
# поэтому в total не добавляется). Биллится дешевле при наличии cache_read.
# ``billed_*`` — подмножество вызовов, для которых известна фактическая цена;
# токены не дублируются: тарифный остаток = input − billed_input и т.д.
_FIELDS = (
    "input", "output", "cached", "calls", "missing_usage_calls",
    "billed_calls", "billed_input", "billed_output", "billed_cached",
)
# Денежные поля ведутся отдельно: int() обрезал бы дробную часть.
_FLOAT_FIELDS = ("cost",)
_ALL_FIELDS = _FIELDS + _FLOAT_FIELDS

# Ниже этого порога денежная величина считается нулём (float-хвосты вычитания).
COST_EPSILON = 1e-9


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def category_for_request_label(request_label: str | None) -> str:
    """Маппинг ``request_label`` вспомогательного вызова → категория."""
    label = (request_label or "").strip().lower()
    if not label:
        return CATEGORY_MAIN
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
    return CATEGORY_MAIN


def _empty_bucket() -> Dict[str, Any]:
    bucket: Dict[str, Any] = {field: 0 for field in _FIELDS}
    for field in _FLOAT_FIELDS:
        bucket[field] = 0.0
    return bucket


class UsageAccount:
    def __init__(self, by_category: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.by_category: Dict[str, Dict[str, Any]] = {}
        if by_category:
            for category, bucket in by_category.items():
                if not isinstance(bucket, dict):
                    continue
                dst = self._bucket(str(category))
                for field in _FIELDS:
                    raw = bucket.get(field, 0)
                    dst[field] = int(raw) if isinstance(raw, (int, float)) else 0
                for field in _FLOAT_FIELDS:
                    raw = bucket.get(field, 0.0)
                    dst[field] = float(raw) if isinstance(raw, (int, float)) else 0.0

    def _bucket(self, category: str) -> Dict[str, Any]:
        bucket = self.by_category.get(category)
        if bucket is None:
            bucket = _empty_bucket()
            self.by_category[category] = bucket
        return bucket

    def add(
        self,
        category: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        cost: Optional[float] = None,
        billed: bool = False,
    ) -> None:
        bucket = self._bucket(category)
        inp = max(0, int(input_tokens or 0))
        out = max(0, int(output_tokens or 0))
        cached = min(max(0, int(cached_tokens or 0)), inp)
        bucket["input"] += inp
        bucket["output"] += out
        bucket["cached"] += cached
        bucket["calls"] += 1
        if billed:
            bucket["billed_calls"] += 1
            bucket["billed_input"] += inp
            bucket["billed_output"] += out
            bucket["billed_cached"] += cached
            bucket["cost"] += float(cost or 0.0)

    def add_missing(self, category: str) -> None:
        """Вызов был, но провайдер не вернул usage: токены не считаем."""
        bucket = self._bucket(category)
        bucket["calls"] += 1
        bucket["missing_usage_calls"] += 1

    def merge(self, other: "UsageAccount", as_category: Optional[str] = None) -> None:
        """Влить чужой счёт. ``as_category`` — свернуть все категории other в одну
        (используется для субагентов: их вклад → категория ``subagent`` родителя)."""
        if not isinstance(other, UsageAccount):
            return
        for category, bucket in other.by_category.items():
            dst = self._bucket(as_category or category)
            for field in _ALL_FIELDS:
                if field in _FLOAT_FIELDS:
                    dst[field] += _num(bucket.get(field, 0.0))
                else:
                    dst[field] += int(bucket.get(field, 0) or 0)

    def delta_from(self, baseline: "UsageAccount") -> "UsageAccount":
        """Новый аккаунт = self − baseline (по полям, не ниже 0).

        Используется для usage именно текущего прогона при resume треда, где
        ``self`` — кумулятив, а ``baseline`` — снимок до старта прогона.
        """
        out = UsageAccount()
        base = baseline.by_category if isinstance(baseline, UsageAccount) else {}
        for category, bucket in self.by_category.items():
            base_bucket = base.get(category, {})
            dst = out._bucket(category)
            has_delta = False
            for field in _ALL_FIELDS:
                value = _num(bucket.get(field, 0)) - _num(base_bucket.get(field, 0))
                if field in _FLOAT_FIELDS:
                    value = max(0.0, value)
                    if value < COST_EPSILON:
                        value = 0.0
                else:
                    value = max(0, int(value))
                dst[field] = value
                if value:
                    has_delta = True
            if not has_delta:
                del out.by_category[category]
        return out

    def totals(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {field: 0 for field in _FIELDS}
        for field in _FLOAT_FIELDS:
            out[field] = 0.0
        for bucket in self.by_category.values():
            for field in _FIELDS:
                out[field] += int(bucket.get(field, 0) or 0)
            for field in _FLOAT_FIELDS:
                out[field] += _num(bucket.get(field, 0.0))
        out["total"] = out["input"] + out["output"]
        return out

    def is_empty(self) -> bool:
        return self.totals()["calls"] == 0

    def to_meta(self) -> Dict[str, Any]:
        return {
            "by_category": {
                category: dict(bucket) for category, bucket in self.by_category.items()
            }
        }

    @classmethod
    def from_meta(cls, raw: Any) -> "UsageAccount":
        if not isinstance(raw, dict):
            return cls()
        by_category = raw.get("by_category")
        if not isinstance(by_category, dict):
            return cls()
        return cls(by_category=by_category)

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"UsageAccount({self.totals()})"
