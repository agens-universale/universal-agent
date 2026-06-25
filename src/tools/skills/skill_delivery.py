"""Текст доставки скилла при явном выборе: харнес инжектит инструкцию SKILL.md
в сообщение агенту, чтобы модели не приходилось грузить главный документ тулом.

Доменный слой скиллов (знает структуру скилла, грузит detail). Презентацию/
проводку добавляют контроллеры (TUI). См. spec 2026-06-25-skill-instruction-injection.
"""
from __future__ import annotations

from typing import Any


def build_skill_delivery(skill_id: str, registry: Any) -> str:
    """Собрать текст доставки скилла для wire-сообщения агенту.

    Директива + инлайн-инструкция SKILL.md + имена references/assets + «замок».
    При невозможности загрузить detail (неизвестный id) — fallback на одну
    директиву (как было): отправка сообщения никогда не ломается из-за скилла.
    """
    directive = f"Используй skill `{skill_id}`."
    try:
        detail = registry.load_skill_detail(skill_id)
    except Exception:
        return directive

    instructions = str((detail or {}).get("instructions") or "").strip()
    refs = list((detail or {}).get("references") or [])
    assets = list((detail or {}).get("assets") or [])

    parts: list[str] = [directive, "", instructions]

    if refs:
        parts.append("")
        parts.append(
            "Доступные references (грузи по точному имени через skills_load_reference): "
            + ", ".join(refs)
        )
    if assets:
        if not refs:
            parts.append("")
        parts.append(
            "Доступные assets (грузи по точному имени через skills_load_asset): "
            + ", ".join(assets)
        )

    lock = [
        f"Skill `{skill_id}` уже загружен — инструкция приведена выше, повторно "
        "skills_load не вызывай."
    ]
    if refs or assets:
        lock.append(
            "Вспомогательные файлы грузи по точным именам выше "
            "(skills_load_reference / skills_load_asset)."
        )
    lock.append("Файлы скилла через read_file не читай.")

    parts.append("")
    parts.append(" ".join(lock))
    return "\n".join(parts)
