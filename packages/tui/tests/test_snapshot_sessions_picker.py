"""/sessions picker — каноничный вид с 7 фикстурными задачами (новый дизайн Spec 1)."""

from gc_testkit.tui_support import tui_settings
import json
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


# Фиксированный "сейчас" для стабильности snapshot'а
_FIXED_NOW = datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)


def _fixed_rel(iso_utc, **_):
    """Детерминированное относительное время в UTC против _FIXED_NOW.

    Не зависит от локали машины (в проде format_relative_local использует
    локальную зону — корректность правил проверяется в test_relative_time)."""
    dt = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00"))
    delta = (_FIXED_NOW - dt).total_seconds()
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    days = (_FIXED_NOW.date() - dt.date()).days
    if days == 1:
        return f"вчера, {dt.strftime('%H:%M')}"
    if 2 <= days <= 6:
        return f"{days} дн назад"
    return f"{dt.day} мая"


def _write_task(
    tmp_path,
    tid,
    *,
    status,
    hours_ago,
    msgs_count,
    skills=None,
    branch=None,
    first_prompt="",
):
    from gc_tui.services.session_title import normalize_prompt_title

    updated = (_FIXED_NOW - timedelta(hours=hours_ago)).isoformat()
    meta = {
        "thread_id": tid,
        "status": status,
        "updated_at": updated,
        "messages_count": msgs_count,
    }
    tui = {}
    if skills:
        tui["skills"] = skills
    if branch:
        tui["branch"] = branch
    if first_prompt:
        # Имя сессии пикер берёт ТОЛЬКО из `tui.last_actual`/`tui.title` и
        # намеренно не сканирует jsonl (см. `session_title`). В проде это поле
        # сеет первый же промпт (`TraceScreen._seed_or_update_last_actual`), и
        # фикстура обязана повторять прод — иначе снимок молча показывает
        # «Без названия» и перестаёт проверять заголовки вовсе.
        tui["last_actual"] = normalize_prompt_title(first_prompt)
    if tui:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    # История — столько сообщений, сколько в meta: число в карточке
    # хранилище считает по истории, а не берёт на веру из meta.
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "type": "message",
                    "seq": n,
                    "ts": updated,
                    "payload": {
                        "id": f"u{n}",
                        "role": "user",
                        "content": first_prompt,
                    },
                }
            )
            + "\n"
            for n in range(1, msgs_count + 1)
        ),
        encoding="utf-8",
    )


def test_sessions_picker_full_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens import sessions as sessions_mod
    from gc_tui.screens.sessions import SessionsScreen

    # Детерминируем относительное время (иначе зависит от часов/локали машины).
    monkeypatch.setattr(sessions_mod, "format_relative_local", _fixed_rel)
    # Текущая ветка фиксирована (иначе зависит от git-состояния машины).
    monkeypatch.setattr(
        sessions_mod, "current_branch", lambda cwd: "feat/tui-mvp-impl-v2"
    )
    # Путь проекта фиксирован (иначе зависит от cwd машины и ОС-разделителей).
    monkeypatch.setattr(
        sessions_mod.SessionsScreen,
        "_project_path",
        lambda self: "D:\\dev\\sber\\background-agent",
    )

    # 8 фикстурных задач разных типов (вкл. очень длинный промпт — проверка клиппинга)
    _write_task(
        tmp_path,
        "10ngpr0m",
        status="stopped",
        hours_ago=0.2,
        msgs_count=42,
        branch="feat/tui-mvp-impl-v2",
        first_prompt="это очень длинный промпт который должен обрезаться в одну "
        "строку с многоточием, а не переноситься на кучу строк и ломать вёрстку "
        "карточки сессии в списке — вот ещё текста чтобы точно переполнить ширину",
    )
    _write_task(
        tmp_path,
        "7f3a9b1c",
        status="running",
        hours_ago=1,
        msgs_count=8,
        branch="feat/tui-mvp-impl-v2",
        first_prompt="почини падающий тест на суммаризации. вот трейс из CI: FAILED tests/test_thread_summary.py",
    )
    _write_task(
        tmp_path,
        "87ae4160",
        status="stopped",
        hours_ago=3,
        msgs_count=14,
        branch="master",
        first_prompt="используй скилл skill-creator нужно создать скилл для создания отчётов",
    )
    _write_task(
        tmp_path,
        "6b966231",
        status="stopped",
        hours_ago=5,
        msgs_count=5,
        branch="feat/tui-mvp-impl-v2",
        first_prompt="расскажи об этом проекте, что он умеет",
    )
    _write_task(
        tmp_path,
        "5237817a",
        status="done",
        hours_ago=24 + 5,
        msgs_count=22,
        branch="feat/rest-api",
        first_prompt="добавь rest-api для управления тредами + тесты",
    )
    _write_task(
        tmp_path,
        "95433b4a",
        status="error",
        hours_ago=24 + 8,
        msgs_count=3,
        first_prompt="объясни как работает chat_completions backend",
    )
    _write_task(
        tmp_path,
        "7cc0417f",
        status="done",
        hours_ago=48 + 5,
        msgs_count=11,
        first_prompt="подготовь статью о паттерне ReAct для блога",
    )
    _write_task(
        tmp_path,
        "d1f4e3a8",
        status="done",
        hours_ago=72 + 8,
        msgs_count=18,
        first_prompt="разобраться с авто-суммаризацией chat_completions",
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )

    async def setup(pilot):
        await pilot.pause()
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli")
        )
        await pilot.pause(0.3)

    assert snap_compare(app, terminal_size=(120, 32), run_before=setup)
