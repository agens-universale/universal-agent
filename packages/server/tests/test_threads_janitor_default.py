"""Фоновая чистка тредов не включается сама.

Что случилось 01.09.2026. Живой `gc-serve` в каталоге проекта удалил треды
старше недели — из `<state>/projects/<slug>/threads`, то есть из того же стора,
куда пишут `gc-cli` и TUI. Ни строчки в лог, даже на DEBUG. Конфиг при этом был
на месте, просто без секции `threads_cleanup`: `... or {}` → `get("enabled", True)`
включал чистку и когда файла нет вовсе, и когда он есть без секции. Второй случай
и есть типичный.

Дефолт `True` пришёл с develop, но там у него не было этой цены: точки входа
`gc-serve` не существовало, а `uvicorn.run("src.server:app")` резолвился только
из корня репозитория — сервер из установленного колеса не запускался в принципе.
Ветка добавила `gc-serve`, и жестом «поднял сервер в своём проекте» стало можно
потерять переписку.

`docs/server.md` при этом обещает обратное: «любые ротации/очистки рекомендуется
настраивать отдельно … вне этого приложения».
"""

from __future__ import annotations

import json
import logging
import os
import time

import gc_server.server_cleanup_threads as janitor
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gc_server.server_cleanup_threads import register_cleanup_threads_on_startup


@pytest.fixture(autouse=True)
def _reset_started():
    """Модульный флаг переживает тесты — иначе второй регистрирует пустышку."""
    janitor._cleanup_thread_started = False
    yield
    janitor._cleanup_thread_started = False


def _store_with_old_thread(tmp_path) -> tuple[str, str]:
    threads = tmp_path / "threads"
    threads.mkdir()
    meta = threads / "meta_generic_old.json"
    meta.write_text(json.dumps({"updated_at": "2026-07-01T00:00:00+00:00"}), encoding="utf-8")
    (threads / "data_generic_old.jsonl").write_text("", encoding="utf-8")
    return str(threads), str(meta)


def _run(server_config, threads_dir, caplog):
    app = FastAPI()
    register_cleanup_threads_on_startup(
        app,
        server_config=server_config,
        base_agent_config={"threads_data_path": threads_dir},
        logger=logging.getLogger("gc_server.server_cleanup_threads"),
    )
    with caplog.at_level(logging.INFO, logger="gc_server.server_cleanup_threads"):
        with TestClient(app):
            # Поток чистит ДО первого sleep; ждём, пока он отработает.
            time.sleep(0.4)
    return caplog.text


@pytest.mark.parametrize(
    "server_config, case",
    [
        ({}, "конфига нет вовсе"),
        ({"server": {"host": "0.0.0.0"}}, "конфиг есть, секции нет"),
    ],
)
def test_janitor_stays_off_without_explicit_enable(tmp_path, caplog, server_config, case):
    """Оба случая, а не только «файла нет».

    Полумера «выключать только при отсутствии конфига» отвергнута сознательно:
    воспроизведение шло с существующим config_server.yml без секции, и это
    типичнее.

    Подмена, которую ловит тест: вернуть `get("enabled", True)`.
    """
    threads_dir, meta = _store_with_old_thread(tmp_path)

    _run(server_config, threads_dir, caplog)

    assert os.path.exists(meta), f"{case}: тред удалён без явного enabled"


def test_janitor_announces_directory_and_ttl(tmp_path, caplog):
    """Включённая чистка называет каталог и срок, и сообщает об удалении.

    Громкость была устроена наоборот: «каталога нет» кричало предупреждением, а
    снос десятка тредов не печатал ничего.

    Подмена: убрать любую из двух строк лога. Отдельно ловится перенос строки
    «включена» на регистрацию хука — там она выполняется до `basicConfig` в
    `serve()` и до глаз не доходит; в тесте это видно как отсутствие записи
    внутри `with TestClient(app)`.
    """
    threads_dir, meta = _store_with_old_thread(tmp_path)

    text = _run({"threads_cleanup": {"enabled": True, "ttl_days": 7}}, threads_dir, caplog)

    assert not os.path.exists(meta), "включённая чистка обязана удалить старый тред"
    assert threads_dir in text, "в логе нет каталога, который чистится"
    assert "ttl" in text.lower(), "в логе нет срока хранения"
    assert "удалено 1" in text, "факт удаления не залогирован"
