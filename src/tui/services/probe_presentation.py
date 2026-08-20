"""Презентация вердикта пробы соединения: строка статуса (автопроба при уходе с
поля, Ctrl+Space вручную) и короткая реплика после сохранения.

Модуль-владелец смысла `ProbeResult` для экранов: и мастер, и /settings читают
вердикт отсюда, а не разбирают поля у себя — разошедшиеся прочтения одного
результата уже давали противоречащие друг другу тексты на одном экране.
Здесь же модельный хвост строки: если проба нашла конкретный id среди моделей
сервера, строка это подтверждает, если нет — честно предупреждает вместо
молчаливого «доступен».

Слой презентации: чистые функции над результатом, без Textual-виджетов."""
from __future__ import annotations

from rich.text import Text

from tui.palette import DEEP_DIM, ERR, OK, WARN


def plural_models(n: int) -> str:
    """1 модель · 2-4 модели · 5+ моделей (11-14 — исключение: «моделей»)."""
    if 11 <= (n % 100) <= 14:
        return "моделей"
    d = n % 10
    if d == 1:
        return "модель"
    if 2 <= d <= 4:
        return "модели"
    return "моделей"


def probe_line_text(result, *, model_id: str = "") -> Text:
    if result is None:
        return Text("— не проверено", style=DEEP_DIM)
    if result.verdict == "ok":
        msg = f"✓ {result.detail} · {result.latency_ms} ms"
        if result.models:
            n = len(result.models)
            msg += f" · {n} {plural_models(n)} на сервере"
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


def save_warning(result) -> str:
    """Короткая реплика рядом с «сохранено», когда проба закончилась не «ок».
    Пустая строка — говорить нечего.

    Смысл берём из `auth_state`/`offline`, а не из `verdict`: «rejected» бывает и
    про имя модели (404 с телом от живого сервера), «unknown» — вердиктом канарейки,
    когда сервер отвечает, просто неотличимо от заведомо неверного токена. Прежние
    тексты («сервер отклонил токен», «сервер недоступен») в обоих случаях врали.
    Остальное отдаём как есть: `detail` уже написан для человека."""
    if result is None or result.verdict == "ok":
        return ""
    if result.auth_state == "rejected":
        return "сервер отклонил токен — проверьте"
    if result.offline:
        return "проверить не удалось — сервер недоступен"
    return result.detail


def token_accepted(result, *, has_token: bool):
    """Приняли ли токен: True / False / None — не проверяли, утверждать нельзя.

    Единственное место, где из результата достают вердикт О ТОКЕНЕ, — и потому
    единственное, где записано, чему верить. Ответ справочника моделей вердиктом не
    является: у OpenRouter `/api/v1/models` публичен и отвечает без токена, так что
    его «200» ничего про токен не говорит. Достоверны два источника: `auth_state`
    (его ставит differential-проба) и отказ 401 на самом маршруте.

    Решает не текст ответа, а МАРШРУТ, который его дал. Список моделей в результате
    значит, что отвечал справочник, — и тогда сказать нечего. Моделей нет — значит
    вердикт пришёл по самому `chat/completions` (проба уходит туда фолбэком), и там
    любой не-401 означает, что запрос прошёл авторизацию и спорит уже о теле: живой
    GigaCode на неверное имя модели отвечает 404, и это «токен в порядке».

    Токена нет вовсе — запрос и был контрольным: ответил сервер осмысленно, значит
    авторизация не требуется; ответил 401 — требуется, а предъявить нечего."""
    if result is None:
        return None
    if result.auth_state in ("accepted", "not_required"):
        return True
    if result.auth_state in ("rejected", "required"):
        return False
    if result.http_status == 401:
        return False
    if result.models is not None:
        return None             # отвечал справочник моделей — он токен не судит
    if not has_token:
        return True if result.verdict == "ok" else None
    return True
