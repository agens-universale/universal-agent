from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping

from gc_core.auto_routing.policy import (
    LEVEL_ORDER, PAIR_LOWER, PAIR_UPPER, SAFE_LEVEL_AUTO, START_LEVEL,
    RoutingParams, pair_levels,
)

AUTO = "auto"


def auto_unavailable_reason(
    source: Any, *, force_legacy: bool, legacy_call_kits: bool = False
) -> str | None:
    """Почему автовыбор недоступен этому конфигу, или None — доступен.
    Единственная точка решения: `auto_available`, стартовый нотис и витрины TUI
    читают причину отсюда, текст один на все входы."""
    from gc_core.settings import settings_of

    cfg = settings_of(source)
    if force_legacy:
        return "уровни отключены флагом --model"
    if not cfg.agent.auto_routing:
        return "выключен настройкой auto_routing"
    if cfg.backend.api_mode != "chat_completions":
        return "режим API responses"
    return _one_model_reason(cfg, legacy_call_kits=legacy_call_kits)


def auto_available(source: Any, *, force_legacy: bool, legacy_call_kits: bool = False) -> bool:
    return auto_unavailable_reason(source, force_legacy=force_legacy, legacy_call_kits=legacy_call_kits) is None


def resolve_routing_params(source: Any, *, legacy_call_kits: bool = False) -> RoutingParams:
    """Параметры автовыбора конфига — единственная точка их сборки для рантайма.

    `safe_level: auto` (пару хода выбирает судья, лестница пар) работает, только когда
    политика `two_level` и в обеих парах — две разные модели. Иначе лестница вырождается:
    с двумя моделями одна из пар — одна модель, и ход то и дело идёт ей целиком (замеры
    pair-judge и раскладки B: то же качество дороже или провал). Тогда `auto` — обычная
    пара из двух разных моделей: нижняя (`default`), если в ней модели разные, иначе
    верхняя (`max`)."""
    params = RoutingParams.from_config(source)
    if params.safe_level != SAFE_LEVEL_AUTO:
        return params
    kits = _level_kits(source)
    lower_two = not _one_call_kit(kits, pair_levels(replace(params, safe_level=PAIR_LOWER)), source=source, legacy=legacy_call_kits)
    upper_two = not _one_call_kit(kits, pair_levels(replace(params, safe_level=PAIR_UPPER)), source=source, legacy=legacy_call_kits)
    if lower_two and upper_two:
        return params
    return replace(params, safe_level=PAIR_UPPER if upper_two and not lower_two else PAIR_LOWER)


def _one_model_reason(cfg: Any, *, legacy_call_kits: bool = False) -> str | None:
    """Автовыбор недоступен, когда рабочая пара резолвится в один комплект вызова.

    Незаданный слот подменяется соседним. Если другая пара даёт две разные
    модели, причина подсказывает соответствующий безопасный уровень.
    """
    kits = _level_kits(cfg)
    if _one_call_kit(kits, LEVEL_ORDER, source=cfg, legacy=legacy_call_kits):
        return "во всех уровнях одна модель"
    params = resolve_routing_params(cfg, legacy_call_kits=legacy_call_kits)
    if params.safe_level == SAFE_LEVEL_AUTO:
        # Лестница пар осталась включённой — значит, в обеих парах разные модели.
        return None
    weak, strong = pair_levels(params)
    if not _one_call_kit(kits, (weak, strong), source=cfg, legacy=legacy_call_kits):
        return None
    reason = f"в паре {weak}+{strong} одна модель"
    # Сильным бывает любой уровень, у которого есть ступень ниже.
    for other in LEVEL_ORDER[1:]:
        other_pair = pair_levels(replace(params, safe_level=other))
        if other != strong and not _one_call_kit(kits, other_pair, source=cfg, legacy=legacy_call_kits):
            return f"{reason}; другая пара — auto_routing_safe_level: {other}"
    return reason


def _level_kits(cfg: Any) -> dict[str, Any]:
    """Комплекты вызова уровней light/default/max. Резолв — тем же `ModelRouter`,
    что у рантайма (фоллбэк незаданных слотов, единственная модель реестра,
    легаси). Уровень, который не резолвится вовсе (битый реестр), в словарь не
    попадает и в сравнениях не участвует. Импорт ленивый: `levels` грузится рано
    (его тянет пакет `gc_core.tools`), а `model_router` — всё дерево конфига."""
    from gc_core.model_router import ModelRouter

    router = ModelRouter(cfg, level_provider=lambda: START_LEVEL)
    kits: dict[str, Any] = {}
    for level in LEVEL_ORDER:
        try:
            kits[level] = router.resolve_level(level)
        except ValueError:
            continue
    return kits


def _one_call_kit(kits: Mapping[str, Any], levels: Iterable[str], *, source: Any, legacy: bool) -> bool:
    """Все резолвящиеся уровни из `levels` дают один комплект вызова."""
    found = [kits[level] for level in levels if level in kits]
    if legacy:
        return all(kit.same_call_kit(found[0]) for kit in found[1:])
    from gc_core.utils.connect_backend import same_main_request

    return all(same_main_request(source, kit, found[0]) for kit in found[1:])


def auto_active(router: Any, live_level: str | None, available: bool) -> bool:
    """Работает ли auto у вызова: Router есть, его сырой режим — `auto`
    (`live_level` — уже прочитанный `router.live_level()`: точка решения читает
    его один раз на вызов), автовыбор доступен конфигу (`available`, он же
    `ctx.auto_available`) и самому Router'у разрешён auto (`auto_allowed` —
    только у непривязанного, спека §5.1). Единственное место условия: его читают
    точка решения и граница хода."""
    return bool(
        router is not None and live_level == AUTO and available
        and getattr(router, "auto_allowed", True)
    )


def effective_level(live_level: str | None, current_level: str | None, available: bool) -> str:
    """Единственное место перевода литерала `auto` в уровень (спека §3)."""
    live = live_level or START_LEVEL
    if live != AUTO:
        return live
    if not available:
        return START_LEVEL
    return current_level or START_LEVEL
