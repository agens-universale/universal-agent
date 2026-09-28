"""Переиспользуемые редакторы полей конфига (спека B, B1).

Общие для OnboardingScreen (линейный драйвер) и SettingsScreen (нелинейный).
Каждый редактор — Vertical-контейнер; дочерние виджеты несут класс onb-dyn
(свап-идиома драйверов). Визуал — классы onb-hint/onb-option/onb-warn,
идентичен онбордингу (визуальный контракт §3)."""

from gc_tui.editors.model import ModelEditor

__all__ = ["ModelEditor"]
