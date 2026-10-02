"""Плоский вид настроек только для чтения (переходный, до удаления).

Изменить настройки можно только явно: ``override_settings`` /
``LoadedSettings.with_overrides`` (новый объект) или ``RuntimeEnv``.
Производное от настроек (определения субагентов, диагностика) кэшируется
рядом с видом — ``derived_cache``, — а не пишется в сами настройки.
"""

from __future__ import annotations

import copy as _copy
from typing import Any, Dict, MutableMapping


class FrozenConfigDict(dict):
    """``dict``, который нельзя изменить; ``dict(x)``/``copy`` — изменяемая копия."""

    def _readonly(self, *_args, **_kwargs):
        raise TypeError(
            "Настройки неизменяемы: используйте override_settings / "
            "LoadedSettings.with_overrides или RuntimeEnv"
        )

    __setitem__ = __delitem__ = _readonly
    update = pop = popitem = setdefault = clear = _readonly
    __ior__ = _readonly

    def copy(self) -> Dict[str, Any]:
        return dict(self)

    def __copy__(self) -> Dict[str, Any]:
        return dict(self)

    def __deepcopy__(self, memo) -> Dict[str, Any]:
        return _copy.deepcopy(dict(self), memo)

    def __reduce__(self):
        # Кэш производных значений не сериализуется: он пересчитывается.
        return (FrozenConfigDict, (dict(self),))


def derived_cache(config_dict: Any) -> MutableMapping[str, Any]:
    """Кэш производных значений этого вида настроек.

    У неизменяемого вида — свой словарь при объекте (копия вида начинает с
    пустого); у плоского словаря (переходно: тесты) — сам словарь, как раньше.
    """
    if isinstance(config_dict, FrozenConfigDict):
        return config_dict.__dict__.setdefault("_derived", {})
    return config_dict


__all__ = ["FrozenConfigDict", "derived_cache"]
