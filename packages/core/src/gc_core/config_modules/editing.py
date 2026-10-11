"""Изменяемый вид frozen-модели конфига — только для нормализации при загрузке.

Модели конфига неизменяемы: прочитанные настройки нельзя поменять на ходу.
Нормализаторы (подстановка дефолта вместо недопустимого значения, абсолютные
пути) пишутся как раньше — присваиваниями, — но работают с ``ModelEdit``, а
результат собирается новой копией: ``edit.result()``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ModelEdit:
    """Собирает присваивания и вложенные правки; исходная модель не меняется."""

    def __init__(self, model: BaseModel) -> None:
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_updates", {})
        object.__setattr__(self, "_children", {})

    def __getattr__(self, name: str) -> Any:
        updates = object.__getattribute__(self, "_updates")
        if name in updates:
            return updates[name]
        children = object.__getattribute__(self, "_children")
        if name in children:
            return children[name]
        value = getattr(object.__getattribute__(self, "_model"), name)
        if isinstance(value, BaseModel):
            child = ModelEdit(value)
            children[name] = child
            return child
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_children").pop(name, None)
        if isinstance(value, ModelEdit):
            value = value.result()
        object.__getattribute__(self, "_updates")[name] = value

    def result(self) -> BaseModel:
        model = object.__getattribute__(self, "_model")
        update = dict(object.__getattribute__(self, "_updates"))
        for name, child in object.__getattribute__(self, "_children").items():
            rebuilt = child.result()
            if rebuilt is not object.__getattribute__(child, "_model"):
                update[name] = rebuilt
        return model.model_copy(update=update) if update else model


def editable(model: BaseModel | ModelEdit) -> ModelEdit:
    """Обернуть модель (или вернуть уже обёрнутую) для нормализации."""
    return model if isinstance(model, ModelEdit) else ModelEdit(model)
