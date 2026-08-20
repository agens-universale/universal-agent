"""Харнесс сравнения прогонов агента: скриптованный бэкенд + дамп треда.

Гейт переезда на async (docs/plans/2026-08-20-async-core.md, Задача 1).
Пока сравнивает синхронный прогон с синхронным — тавтология; смысл появится,
когда вторым аргументом сравнения встанет ``AgentExecutor.arun``. Но
инфраструктура сравнения обязана существовать ДО переезда, а не после: иначе
доказывать эквивалентность будет нечем.

Лежит соседним модулем, а не пакетом ``parity/``: у подкаталогов наборов в
этом репозитории нет ``__init__.py``, и относительный импорт из теста не
сработает. Каталог набора и так на sys.path — тесты ядра тем же способом берут
соседей (``from test_project_data_backend import ...``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


class _Response:
    """Ответ, совместимый и с requests (sync), и с httpx (async, Фаза 1)."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload

    def iter_lines(self) -> Iterable[bytes]:
        yield f"data: {json.dumps(self._payload, ensure_ascii=False)}".encode()
        yield b"data: [DONE]"

    async def aiter_lines(self):
        yield f"data: {json.dumps(self._payload, ensure_ascii=False)}"
        yield "data: [DONE]"


class ScriptedBackend:
    """Ходы модели по порядку, независимо от payload.

    Лишний вызов — явная ошибка теста, а не молчаливое зацикливание: сценарий
    из N ходов должен исполниться ровно N раз, иначе сравнивать нечего.
    """

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)
        self._i = 0
        self.calls: list[dict] = []
        #: Сколько раз сценарий просили за пределами длины. Считаем отдельно,
        #: потому что исключение отсюда до теста НЕ доходит: для цикла агента
        #: ошибка бэкенда — повод для ретрая, и он её проглатывает. Без счётчика
        #: переполнение выглядело бы как обычный прогон.
        self.overflow = 0

    def assert_clean(self) -> None:
        """Сценарий сошёлся: ни перебора, ни недобора ходов."""
        assert self.overflow == 0, (
            f"сценарий исчерпан: {len(self._turns)} ходов, лишних запросов "
            f"{self.overflow}"
        )
        assert self._i == len(self._turns), (
            f"сценарий недобран: исполнено {self._i} из {len(self._turns)} ходов"
        )

    def _next(self, payload: dict | None) -> _Response:
        self.calls.append(payload or {})
        if self._i >= len(self._turns):
            self.overflow += 1
            raise AssertionError(
                f"ScriptedBackend: сценарий исчерпан ({len(self._turns)} ходов), "
                f"пришёл вызов №{self._i + 1}"
            )
        turn = self._turns[self._i]
        self._i += 1
        return _Response(turn)

    # requests.Session.post
    def post(self, url: str, json: dict | None = None, **kw: Any) -> _Response:
        return self._next(json)


def install(monkeypatch, backend: ScriptedBackend) -> ScriptedBackend:
    """Подменить синхронный транспорт. Async-двойник появится в Задаче 9."""
    from gc_core.utils import connect_backend

    monkeypatch.setattr(connect_backend._NO_NETRC_SESSION, "post", backend.post)
    return backend


def assistant_tool_call(name: str, arguments: dict, call_id: str = "c1") -> dict:
    """Ход модели «вызвать тул» в формате chat/completions."""
    return {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name,
                                 "arguments": json.dumps(arguments, ensure_ascii=False)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def assistant_text(text: str) -> dict:
    """Ход модели «ответить текстом»."""
    return {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
    }


def judge_accept() -> dict:
    """Ход судьи finish-gate: сырой JSON-вердикт «принято».

    После финализатора цикл идёт к модели ещё раз — прогнать two-axis finish
    gate. Судья ждёт ГОЛЫЙ JSON с ключами action/trace_ok/report_ok/is_question
    (все четыре обязательны — без is_question ответ считается битым); вызов тула
    он считает невалидным ответом и переспрашивает, а сценарий из-за лишнего
    хода перестаёт сходиться.
    """
    return assistant_text(
        '{"action": "accept", "trace_ok": true, "report_ok": true, '
        '"is_question": false}'
    )


def parity_config(tmp_path: Path, monkeypatch, **overrides: Any) -> dict:
    """Полный конфиг прогона — через настоящий ``load_config``, а не руками.

    Собирать словарь по ключам вручную нельзя: цикл агента читает десятки
    значений (``backend_request_timeout_sec``, лимиты суммаризации, политики
    безопасности), и любой пропущенный всплывёт как KeyError посреди прогона.
    Пусть дефолты приезжают оттуда же, откуда в проде.

    Слои поверх базового конфига нейтрализованы: без этого прогон читал бы
    настоящие ``~/.gc/config.local.yml`` и ``<cwd>/.gc/config.yml``, и
    воспроизводимость зависела бы от машины.
    """
    from gc_core import config as cfgmod

    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfgmod, "_project_local_config_path", lambda: None)
    # Патчим ИМЯ, связанное в самом config.py: `from ... import managed_config_path`
    # связывает его на импорте, и подмена в модуле-источнике не подействует.
    monkeypatch.setattr(cfgmod, "managed_config_path",
                        lambda: tmp_path / "absent.local.yml")

    prompt = tmp_path / "system_prompt.md"
    prompt.write_text("SYSTEM PROMPT", encoding="utf-8")

    conf = tmp_path / "config.yml"
    conf.write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        "  completions_url: \"http://parity.invalid/v1/chat/completions\"\n"
        "  use_retry: false\n"
        "agent:\n"
        "  model: parity-model\n"
        "  use_local_subagents: false\n"
        "  max_steps: 5\n"
        "logging:\n"
        "  console_trace: false\n"
        "  save_logs: false\n",
        encoding="utf-8",
    )
    cfg = cfgmod.load_config(str(conf))
    cfg["threads_data_path"] = str(tmp_path / "threads")
    cfg["logs_dump_dir"] = str(tmp_path / "logs")
    cfg["launch_root"] = str(tmp_path)
    cfg["chat_completions_system_prompt_path"] = str(prompt)
    cfg["console_trace"] = False
    cfg.update(overrides)
    return cfg


#: Поля, которые меняются от прогона к прогону и сравнению не подлежат.
#: Не «шум»: id и call_id генерируются от содержимого и времени, таймстемпы —
#: от часов. Всё остальное обязано совпадать байт в байт.
_VOLATILE_KEYS = ("id", "call_id", "created", "timestamp", "ts", "run_id",
                  "trace_id", "message_id", "response_id")


def strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in _VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(x) for x in obj]
    return obj


def run_thread_dump(config_dict: dict, system_id: str, thread_id: str) -> list[dict]:
    """Дамп ``data_<system>_<thread>.jsonl`` без недетерминированных полей.

    Путь считает ``thread_store`` — повторять его формат здесь нельзя: имя
    файла уже менялось (legacy-формат без system), и копия разъехалась бы молча.
    """
    from gc_core.thread_store import _thread_paths, resolve_threads_dir

    data_path, _meta = _thread_paths(resolve_threads_dir(config_dict),
                                     system_id, thread_id)
    path = Path(data_path)
    if not path.exists():
        return []
    return [strip_volatile(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
