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

    def __enter__(self) -> _Response:
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
            f"сценарий исчерпан: {len(self._turns)} ходов, лишних запросов {self.overflow}"
        )
        assert self._i == len(self._turns), (
            f"сценарий недобран: исполнено {self._i} из {len(self._turns)} ходов"
        )

    def _next(self, payload: dict | None) -> _Response:
        self.calls.append(payload or {})
        if self._i >= len(self._turns):
            self.overflow += 1
            raise AssertionError(
                f"ScriptedBackend: сценарий исчерпан ({len(self._turns)} ходов), пришёл вызов №{self._i + 1}"
            )
        turn = self._turns[self._i]
        self._i += 1
        return _Response(turn)

    def next_payload(self, request_payload: dict | None) -> dict:
        """Следующий ход сценария как словарь ответа модели."""
        return self._next(request_payload)._payload

    # requests.Session.post — оставлено для прямых тестов транспорта
    def post(self, url: str, json: dict | None = None, **kw: Any) -> _Response:
        return self._next(json)


def _as_sse(turn: dict) -> str:
    """Разложить готовый ответ модели в SSE-чанки.

    Сценарии написаны в форме не-стримового ответа (так их проще читать), а
    транспорт умеет обе формы. Перевод здесь, чтобы сценарий был один на оба
    пути: иначе стримовый и не-стримовый прогоны сравнивались бы по разным
    входным данным, и гейт паритета ничего бы не значил.
    """
    choice = (turn.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    chunks: list[dict] = []
    base = {
        "id": turn.get("id", "scripted"),
        "model": turn.get("model", "scripted"),
    }

    content = message.get("content")
    if content:
        chunks.append(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content},
                    }
                ],
            }
        )
    for i, call in enumerate(message.get("tool_calls") or []):
        chunks.append(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": i,
                                    "id": call.get("id"),
                                    "type": call.get("type", "function"),
                                    "function": call.get("function", {}),
                                }
                            ]
                        },
                    }
                ],
            }
        )
    chunks.append(
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": choice.get("finish_reason") or "stop",
                }
            ],
            "usage": turn.get("usage"),
        }
    )
    body = "".join(
        f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks
    )
    return body + "data: [DONE]\n\n"


def install(monkeypatch, backend: ScriptedBackend) -> ScriptedBackend:
    """Подменить транспорт запросов к модели.

    С Задачи 9 путь к модели один — httpx.AsyncClient (синхронный вход остался
    шимом через портал), поэтому подменяется фабрика клиента, а не приватная
    requests-сессия. Один установщик обслуживает и синхронный прогон, и
    асинхронный: гейт паритета обязан кормить оба одним и тем же сценарием.
    """
    import httpx
    from gc_core.utils import connect_backend

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        turn = backend.next_payload(payload)
        if payload.get("stream"):
            # Вызывающий попросил стрим (это делает TUI и AsyncAgent через
            # on_stream_delta) — отдаём тот же ход в форме SSE. Иначе сборка
            # получит JSON вместо чанков и сочтёт ответ пустым.
            return httpx.Response(
                200,
                text=_as_sse(turn),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=turn)

    if hasattr(connect_backend, "get_async_client"):
        monkeypatch.setattr(
            connect_backend,
            "get_async_client",
            lambda **kw: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
    else:
        # Синхронная ветка (develop) ходит к модели через requests-сессию.
        # Ветка нужна, чтобы ОДИН харнесс снимал эталон и там, и здесь: иначе
        # сравнивались бы прогоны, собранные разными установщиками, и гейт
        # доказывал бы меньше, чем обещает. `ScriptedBackend` держит совместимый
        # с `requests` `post` ровно для этого случая.
        monkeypatch.setattr(connect_backend, "_NO_NETRC_SESSION", backend)
    return backend


def install_many(monkeypatch, backends: dict) -> dict:
    """Подменить транспорт НЕСКОЛЬКИМИ сценариями, по одному на модель.

    Один общий сценарий на двух агентов не годится: ходы раздаются в порядке
    ВЫЗОВОВ, и второй агент забирает финализатор первого. Такой тест меряет не
    параллельность, а очередь — что и случилось при первой попытке измерить
    Задачу 26.

    ``backends`` — ``{имя_модели: ScriptedBackend}``; конфиги агентов должны
    отличаться полем ``model``.
    """
    import httpx
    from gc_core.utils import connect_backend

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        model = payload.get("model")
        backend = backends.get(model)
        if backend is None:
            raise AssertionError(
                f"нет сценария для модели {model!r}; есть: {sorted(backends)}"
            )
        turn = backend.next_payload(payload)
        if payload.get("stream"):
            return httpx.Response(
                200,
                text=_as_sse(turn),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=turn)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return backends


def assistant_tool_call(
    name: str, arguments: dict, call_id: str = "c1"
) -> dict:
    """Ход модели «вызвать тул» в формате chat/completions."""
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    arguments, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def assistant_text(text: str) -> dict:
    """Ход модели «ответить текстом»."""
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 2,
            "total_tokens": 14,
        },
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
        '{"action": "accept", "trace_ok": true, "report_ok": true, "is_question": false}'
    )


def parity_config(tmp_path: Path, monkeypatch, **overrides: Any) -> Any:
    """Полные настройки прогона (``LoadedSettings``) — через настоящий
    ``load_settings``, а не руками.

    Собирать настройки по ключам вручную нельзя: цикл агента читает десятки
    значений (``backend.request_timeout_sec``, лимиты суммаризации, политики
    безопасности), и любой пропущенный разошёлся бы с продом посреди прогона.
    Пусть дефолты приезжают оттуда же, откуда в проде.

    Слои поверх базового конфига нейтрализованы: без этого прогон читал бы
    настоящие ``~/.gc/config.local.yml`` и ``<cwd>/.gc/config.yml``, и
    воспроизводимость зависела бы от машины.

    ``overrides`` — пути через точку (``**{"agent.max_steps": 1}``); см.
    ``with_path_overrides``.
    """
    from gc_core import config as cfgmod

    conf, prompt = _parity_files(tmp_path, monkeypatch)
    loaded = cfgmod.load_settings(str(conf))
    loaded = loaded.with_overrides(
        {
            "paths.threads_data_path": str(tmp_path / "threads"),
            "paths.logs_dump_dir": str(tmp_path / "logs"),
            "paths.chat_completions_system_prompt_path": str(prompt),
            "logging.console_trace": False,
        }
    ).with_runtime(launch_root=str(tmp_path))
    return with_path_overrides(loaded, overrides)


def _parity_files(tmp_path: Path, monkeypatch) -> tuple:
    """Базовый ``config.yml`` и системный промпт прогона; слои выше — нейтрально."""
    from gc_core.config_modules import env_vars as _env_vars

    tmp_path.mkdir(parents=True, exist_ok=True)
    # Оба слоя адресуются ОДНИМ модулем-владельцем: алиас
    # `config._project_local_config_path` develop снёс, а `managed_config_path`
    # эта ветка перенесла из `mcp_runtime.paths` в `config_modules.env_vars`
    # (путь принадлежит конфигу). `config.py` берёт обе функции через модуль,
    # а не связанным именем, поэтому подмена у владельца действует.
    monkeypatch.setattr(_env_vars, "project_local_config_path", lambda: None)
    monkeypatch.setattr(
        _env_vars, "managed_config_path", lambda: tmp_path / "absent.local.yml"
    )
    prompt = tmp_path / "system_prompt.md"
    prompt.write_text("SYSTEM PROMPT", encoding="utf-8")
    conf = tmp_path / "config.yml"
    conf.write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        '  completions_url: "http://parity.invalid/v1/chat/completions"\n'
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
    return conf, prompt


#: Рантайм-ключи (то, что вычисляет запуск, а не задаёт файл).
_RUNTIME_KEYS = {
    "launch_root",
    "project_root",
    "trace_id",
    "text_answer_mode",
    "finish_gate_smart",
    "os_backend",
}


def with_path_overrides(loaded: Any, overrides: dict) -> Any:
    """``LoadedSettings`` с правками по путям через точку: рантайм-ключи — в
    ``RuntimeEnv``, остальное — в настройки с валидацией схемы (негодное
    значение — ``ValidationError``).

    Правка ЗАМЕНЯЕТ значение по пути (как прежняя правка плоского словаря) и
    идёт без ``resolve_paths``: производные значения загрузки (например
    ``summary.incremental_batch_chars`` от порога) не пересчитываются — ровно
    как прежде, когда плоский вид правили после ``load_config``. Иначе прогон
    матрицы разошёлся бы с эталоном не из-за кода, а из-за стенда.
    """
    import copy
    from dataclasses import replace

    from gc_core.config_modules.root import AppConfig
    from gc_core.settings import _alias_path

    runtime = {k: v for k, v in overrides.items() if k in _RUNTIME_KEYS}
    updates = {k: v for k, v in overrides.items() if k not in _RUNTIME_KEYS}
    if updates:
        document = loaded.settings.model_dump(by_alias=True)
        for dotted, value in updates.items():
            keys = _alias_path(AppConfig, dotted.split("."))
            node = document
            for key in keys[:-1]:
                nxt = node.get(key)
                node[key] = nxt = dict(nxt) if isinstance(nxt, dict) else {}
                node = nxt
            node[keys[-1]] = copy.deepcopy(value)
        loaded = replace(loaded, settings=AppConfig.model_validate(document))
    if runtime:
        loaded = loaded.with_runtime(**runtime)
    return loaded


#: Поля, которые меняются от прогона к прогону и сравнению не подлежат.
#: Не «шум»: id и call_id генерируются от содержимого и времени, таймстемпы —
#: от часов. Всё остальное обязано совпадать байт в байт.
_VOLATILE_KEYS = (
    "id",
    "call_id",
    "created",
    "timestamp",
    "ts",
    "run_id",
    "trace_id",
    "message_id",
    "response_id",
)


def strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: strip_volatile(v)
            for k, v in obj.items()
            if k not in _VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [strip_volatile(x) for x in obj]
    return obj


def run_thread_dump(
    settings: Any, system_id: str, thread_id: str
) -> list[dict]:
    """Дамп ``data_<system>_<thread>.jsonl`` без недетерминированных полей.

    ``settings`` — настройки прогона (всё, что принимает ``settings_of``).
    Путь считает ``thread_store`` — повторять его формат здесь нельзя: имя
    файла уже менялось (legacy-формат без system), и копия разъехалась бы молча.
    """
    from gc_core.thread_store import _thread_paths, resolve_threads_dir

    data_path, _meta = _thread_paths(
        resolve_threads_dir(settings), system_id, thread_id
    )
    path = Path(data_path)
    if not path.exists():
        return []
    return [
        strip_volatile(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
