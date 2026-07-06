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
    directive = f"Use skill `{skill_id}`."
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
            "Available references (load by exact name via skills_read_reference): "
            + ", ".join(refs)
        )
    if assets:
        if not refs:
            parts.append("")
        parts.append(
            "Available assets (load by exact name via skills_read_asset): "
            + ", ".join(assets)
        )

    lock = [
        f"Skill `{skill_id}` is already loaded — the instruction is shown above; "
        "do not call skills_load again."
    ]
    if refs or assets:
        lock.append(
            "Load auxiliary files by the exact names above "
            "(skills_read_reference / skills_read_asset)."
        )
    lock.append("Do not read skill files via read_file.")

    parts.append("")
    parts.append(" ".join(lock))
    return "\n".join(parts)


def skill_is_available(skill_id: str, registry: Any) -> bool:
    """True, если скилл реально загрузим — для честного маркера «загружен»/«не
    найден». Критерий тот же, что у build_skill_delivery: detail грузится ⟺
    инструкция инжектнута (fallback ⟺ не найден)."""
    try:
        registry.load_skill_detail(skill_id)
        return True
    except Exception:
        return False
