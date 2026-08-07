"""Рендер строки статуса пробы соединения (автопроба при уходе с поля, Ctrl+Space вручную) — используется онбордингом
(шаг «Подключение»). Тело перенесено из бывшего ModelEntryForm._probe_line_text
без изменения текстов; здесь же — модельный хвост: если проба нашла конкретный
id среди моделей сервера, строка это подтверждает, если нет — честно
предупреждает вместо молчаливого «доступен»."""
from __future__ import annotations

from rich.text import Text

from tui.editors.model import _plural_models
from tui.palette import DEEP_DIM, ERR, OK, WARN


def probe_line_text(result, *, model_id: str = "") -> Text:
    if result is None:
        return Text("— не проверено", style=DEEP_DIM)
    if result.verdict == "ok":
        msg = f"✓ {result.detail} · {result.latency_ms} ms"
        if result.models:
            n = len(result.models)
            msg += f" · {n} {_plural_models(n)} на сервере"
        if model_id and result.models is not None:
            ids = {getattr(m, "id", None) for m in result.models}
            if model_id in ids:
                msg += f" · {model_id} найдена на сервере"
            else:
                return Text(f"⚠ модель {model_id} на сервере не найдена", style=WARN)
        return Text(msg, style=OK)
    if result.verdict == "rejected":
        return Text(f"✗ {result.detail}", style=ERR)
    return Text(f"⚠ не удалось проверить: {result.detail}", style=WARN)
