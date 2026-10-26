"""Каталог ручек поверхностей gc-serve: описание для Swagger и группы для
``<поверхность>.disabled_routes``.

Только данные — без FastAPI: каталог читает и проверка config_server.yml
(имя группы или путь в ``disabled_routes`` должны найтись здесь). Проверку
запросов каталог не меняет: тела и параметры ручки разбирают сами, здесь —
их описание (``api_docs`` накладывает его на схему OpenAPI).
Что в каталоге совпадает с ручками приложения, стережёт
``tests/test_api_docs.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JSON = "application/json"


@dataclass(frozen=True)
class Group:
    """Группа ручек: тег в Swagger и имя в ``disabled_routes``."""

    tag: str
    description: str


@dataclass(frozen=True)
class RouteDoc:
    group: str
    summary: str
    description: str = ""
    body: dict[str, Any] | None = None
    parameters: tuple[dict[str, Any], ...] = ()
    responses: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SurfaceDoc:
    title: str
    description: str
    groups: dict[str, Group]
    routes: dict[tuple[str, str], RouteDoc]


# --- общие куски ------------------------------------------------------------


def _json(schema: dict, example: Any = None, **examples: Any) -> dict:
    content: dict[str, Any] = {"schema": schema}
    if example is not None:
        content["example"] = example
    if examples:
        content["examples"] = {
            name: {"value": value} for name, value in examples.items()
        }
    return {"content": {JSON: content}}


def _answer(description: str, schema: dict | None = None, example=None):
    if schema is None and example is None:
        return {"description": description}
    return {"description": description, **_json(schema or {}, example)}


def _query(name: str, description: str, schema: dict | None = None) -> dict:
    return {
        "name": name,
        "in": "query",
        "required": False,
        "description": description,
        "schema": schema or {"type": "string"},
    }


def _path(name: str, description: str) -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "description": description,
        "schema": {"type": "string"},
    }


def _header(name: str, description: str) -> dict:
    return {
        "name": name,
        "in": "header",
        "required": False,
        "description": description,
        "schema": {"type": "string"},
    }


_NO_TASK = "Нет задачи"
_AUTH_MISSING = (
    "Задан `auth_token`, а заголовка `Authorization: Bearer <token>` нет "
    "или он чужой"
)
_TASK_ID_EXAMPLE = "52717e9d-…"
_BAD_BODY = "Негодное тело"
_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_OBJ = {"type": "object"}


def _object(**properties: dict) -> dict:
    return {"type": "object", "properties": properties}


def _described(schema: dict, description: str) -> dict:
    return {**schema, "description": description}


# --- задачи: вебхук и swarm ---------------------------------------------------

_AUTH_403 = _answer(
    _AUTH_MISSING,
    _object(status=_STR, detail=_STR),
    {"status": "forbidden", "detail": "invalid or missing token"},
)
_TASK_ERROR = _object(
    status=_described(_STR, "`error`"),
    payload=_described(_STR, "что не так"),
)
_TASK_PATH = (
    _path("system", "Система задачи (`generic`, `ci`, …)"),
    _path("task_id", "Id задачи"),
)
_STATUS = _object(
    status=_described(
        _STR,
        "`queued` | `running` | `completed` | `failed` | `cancelled` | "
        "`interrupted`",
    ),
    system=_STR,
    task_id=_STR,
    thread_id=_STR,
    started_at=_described(_NUM, "секунды эпохи"),
    last_activity_at=_described(_NUM, "у `running`: последняя запись хода"),
    finished_at=_described(_NUM, "у финальных статусов"),
    result=_described(_STR, "у `completed`: ответ последнего хода прогона"),
    error=_described(_STR, "у `failed`: repr исключения"),
    pending_messages=_described(
        _INT, "сообщения входящего потока, ещё не доставленные агенту"
    ),
)


def _task_body(callbacks: bool) -> dict:
    properties = {
        "system": _described(
            _STR,
            "Система задачи; нет — `webhook.default_system` (`generic`). "
            "Системы других поверхностей, интерактивных сессий и SDK "
            "(`a2a`, `responses`, `completions`, `acp`, `cli`, `tui`, "
            "`sdk`) — 400.",
        ),
        "task_id": _described(
            _STR,
            "Id задачи. Нет — сгенерирует сервер (с `Idempotency-Key` — "
            "`idem-<хеш ключа>`). Сообщение в ту же задачу — тот же "
            "`task_id`: в идущий ход оно попадёт на следующем шаге.",
        ),
        "message": _described(
            _STR,
            "Текст агенту. Без `message` агенту уходит всё тело JSON.",
        ),
        "metadata": _described(
            _OBJ,
            "Объект: дописывается к тексту строкой `metadata: <json>`.",
        ),
        "trace_id": _described(
            _STR, "Сквозной id трассировки прогона; в текст не попадает."
        ),
    }
    if callbacks:
        properties["callback_url"] = _described(
            {"type": "string", "format": "uri"},
            "Куда прислать событие конца прогона (Standard Webhooks). "
            "Только под `webhook.callbacks.allowed_urls`.",
        )
    examples = {
        "message": {
            "system": "ci",
            "task_id": "build-42",
            "message": "почини тест",
        },
        "metadata": {
            "task_id": "build-42",
            "message": "почини тест",
            "metadata": {"ticket": "BUG-1"},
            "trace_id": "wf-1",
        },
    }
    if callbacks:
        examples["callback"] = {
            **examples["metadata"],
            "callback_url": "https://hooks.example/ci/build-42",
        }
    return {
        "required": True,
        "description": "JSON-объект без повторов ключей.",
        **_json(
            {
                "type": "object",
                "properties": properties,
                "additionalProperties": True,
            },
            **examples,
        ),
    }


def _task_routes(*, callbacks: bool, runner: str) -> dict:
    accepted = _answer(
        "Сообщение принято: лежит во входящем потоке треда",
        _object(
            status=_described(
                _STR,
                "`started` — запрос строит прогон; `queued` — тред уже "
                "ведут, ход заберёт сообщение сам; `duplicate` — повтор по "
                "ключу идемпотентности, уже доставлено",
            ),
            system=_STR,
            task_id=_STR,
            message_seq=_described(_INT, "номер сообщения во входящем потоке"),
        ),
        {
            "status": "started",
            "system": "ci",
            "task_id": "build-42",
            "message_seq": 1,
        },
    )
    page_query = (
        _query(
            "cursor",
            "Непрозрачный курсор из прошлого ответа (`next_cursor`).",
        ),
        _query(
            "limit",
            "Сколько записей, до 1000.",
            {"type": "integer", "minimum": 1, "maximum": 1000},
        ),
    )
    return {
        ("POST", "/v1/tasks"): RouteDoc(
            group="tasks",
            summary="Сообщение задаче",
            description=(
                f"Новая задача или сообщение в идущую. {runner} Сообщение "
                "сразу ложится во входящий поток треда — с этого момента оно "
                "принято; у треда в каждый момент не больше одного хода на "
                "любом инстансе. Статус — `GET /v1/tasks/{system}/{task_id}`."
            ),
            body=_task_body(callbacks),
            parameters=(
                _header(
                    "Idempotency-Key",
                    "Повтор с тем же телом — тот же `message_seq`, второго "
                    "сообщения нет; тот же ключ с другим телом — 422.",
                ),
                _header(
                    "webhook-id",
                    "Standard Webhooks: id сообщения (ключ идемпотентности "
                    "при включённой подписи).",
                ),
                _header(
                    "webhook-timestamp",
                    "Standard Webhooks: секунды эпохи.",
                ),
                _header(
                    "webhook-signature",
                    "Standard Webhooks: `v1,<base64 HMAC-SHA256>`; обязательна "
                    "при `webhook.signing_secret`.",
                ),
            ),
            responses={
                "202": accepted,
                "400": _answer(_BAD_BODY, _TASK_ERROR),
                "401": _answer("Нет подписи или она чужая (signing_secret)"),
                "403": _AUTH_403,
                "409": _answer(
                    "Тред не задача вебхука (другой поверхности или "
                    "приложения) или задача занята запросом с другим "
                    "`callback_url`",
                    _TASK_ERROR,
                ),
                "413": _answer("Тело больше `max_body_bytes`"),
                "422": _answer(
                    "Тот же `Idempotency-Key` с другим телом", _TASK_ERROR
                ),
            },
        ),
        ("GET", "/v1/tasks"): RouteDoc(
            group="tasks",
            summary="Список задач",
            description="Сначала недавно обновлённые; страницами по курсору.",
            parameters=(
                _query("system", "Только задачи этой системы."),
                *page_query,
            ),
            responses={
                "200": _answer(
                    "Страница задач",
                    _object(
                        tasks={"type": "array", "items": _STATUS},
                        next_cursor=_described(
                            _STR, "курсор следующей страницы или null"
                        ),
                    ),
                ),
                "400": _answer("Негодный курсор или limit", _TASK_ERROR),
                "403": _AUTH_403,
            },
        ),
        ("GET", "/v1/tasks/{system}/{task_id}"): RouteDoc(
            group="tasks",
            summary="Статус задачи",
            description=(
                "Из хранилища, одинаковый на любом инстансе. "
                "`queued` — сообщение принято, прогон ещё не взял задачу. "
                "`interrupted` — ход прерван падением инстанса и подхвачен "
                "восстановлением (сразу сменится на `running`)."
            ),
            parameters=_TASK_PATH,
            responses={
                "200": _answer(
                    "Статус",
                    _STATUS,
                    {
                        "status": "completed",
                        "system": "ci",
                        "task_id": "build-42",
                        "thread_id": "build-42",
                        "started_at": 1792537100.0,
                        "finished_at": 1792537200.0,
                        "result": "тест починен",
                        "pending_messages": 0,
                    },
                ),
                "403": _AUTH_403,
                "404": _answer("У задачи нет ни статуса, ни брони"),
            },
        ),
        ("GET", "/v1/tasks/{system}/{task_id}/messages"): RouteDoc(
            group="history",
            summary="История задачи",
            description=(
                "Страницами по курсору. Курсор непрозрачен: после сжатия или "
                "пересоздания треда ответ несёт `reset: true` и историю новой "
                "версии с начала."
            ),
            parameters=(*_TASK_PATH, *page_query),
            responses={
                "200": _answer(
                    "Страница истории",
                    _object(
                        messages={
                            "type": "array",
                            "items": _object(
                                seq=_INT,
                                ts=_STR,
                                message=_described(
                                    _OBJ,
                                    "запись истории в форме responses "
                                    "(`message`, `function_call`, "
                                    "`function_call_output`, `reasoning`)",
                                ),
                            ),
                        },
                        next_cursor=_STR,
                        has_more={"type": "boolean"},
                        reset={"type": "boolean"},
                    ),
                ),
                "400": _answer("Негодный курсор", _TASK_ERROR),
                "403": _AUTH_403,
                "404": _answer(_NO_TASK),
            },
        ),
        ("POST", "/v1/tasks/{system}/{task_id}/cancel"): RouteDoc(
            group="tasks",
            summary="Отменить ход",
            description=(
                "Запись в поток `control` треда — ход видит её на любом "
                "инстансе на следующем шаге; итог — `cancelled`. Тело не "
                "нужно; без `Authorization` нужен `Content-Type: "
                "application/json` (защита от запросов со сторонних "
                "страниц)."
            ),
            parameters=_TASK_PATH,
            responses={
                "202": _answer(
                    "Отмена записана",
                    _object(
                        status=_described(
                            _STR,
                            "`cancelling`; у завершённой задачи — её статус",
                        )
                    ),
                    {"status": "cancelling"},
                ),
                "403": _AUTH_403,
                "404": _answer(_NO_TASK),
            },
        ),
        ("GET", "/v1/tasks/{system}/{task_id}/events"): RouteDoc(
            group="history",
            summary="События задачи (SSE)",
            description=(
                "`text/event-stream`: `event: message` — новая запись истории "
                "(`id:` — курсор после неё), `event: reset` — история "
                "заменена, дальше с начала новой версии, `event: status` — "
                "смена статуса. Продолжить — `cursor` или `Last-Event-ID`; "
                "поток кончается на финальном статусе."
            ),
            parameters=(
                *_TASK_PATH,
                _query("cursor", "Курсор, с которого продолжить."),
                _header("Last-Event-ID", "То же, что `cursor` (EventSource)."),
            ),
            responses={
                "200": {
                    "description": "Поток событий",
                    "content": {"text/event-stream": {"schema": _STR}},
                },
                "403": _AUTH_403,
                "404": _answer(_NO_TASK),
            },
        ),
        ("GET", "/healthcheck"): RouteDoc(
            group="service",
            summary="Проверка живости",
            responses={"200": _answer("Жив", _OBJ, {"status": "ok"})},
        ),
    }


_TASK_GROUPS = {
    "tasks": Group("Задачи", "Сообщение задаче, список, статус, отмена хода."),
    "history": Group(
        "История", "История треда задачи страницами и потоком событий."
    ),
    "service": Group("Служебное", "Проверка живости."),
}

_AUTH_NOTE = (
    "**Авторизация.** При заданном `auth_token` каждый запрос несёт "
    "`Authorization: Bearer <token>` (кнопка Authorize), иначе 403. "
    "POST — только `Content-Type: application/json`."
)

WEBHOOK = SurfaceDoc(
    title="GigaCode · Вебхук",
    description=(
        "Задачи внешних систем: сообщение ложится во входящий поток треда "
        "`(system, task_id)`, ход агента ведёт инстанс, взявший аренду "
        "треда; статус и история — из хранилища, одинаковые на любом "
        "инстансе. Упавший посреди хода инстанс подхватывает "
        "восстановление (`webhook.recovery`).\n\n"
        f"{_AUTH_NOTE}\n\nПодробно — docs/server.md, раздел «Вебхук»."
    ),
    groups=_TASK_GROUPS,
    routes=_task_routes(
        callbacks=True,
        runner="Ход ведёт агент в процессе сервера.",
    ),
)

_SWARM_ROUTES = _task_routes(
    callbacks=False,
    runner=(
        "Задачу исполняет `gc-cli` в своей песочнице bubblewrap (только "
        "Linux); повтор `task_id` после конца — 409."
    ),
)
_SWARM_ROUTES[("GET", "/v1/tasks/{system}/{task_id}/output")] = RouteDoc(
    group="history",
    summary="Вывод песочницы",
    description="stdout и stderr процесса песочницы задачи.",
    parameters=_TASK_PATH,
    responses={
        "200": _answer(
            "Вывод",
            _object(stdout=_STR, stderr=_STR),
            {"stdout": "…", "stderr": ""},
        ),
        "403": _AUTH_403,
        "404": _answer(_NO_TASK),
    },
)

SWARM = SurfaceDoc(
    title="GigaCode · Swarm",
    description=(
        "Задачи в изолированных песочницах: та же API `/v1/tasks`, что у "
        "вебхука, но каждая задача — отдельный процесс `gc-cli` в "
        "bubblewrap со своим каталогом `/work` и лимитами памяти, диска и "
        "CPU. Сообщение в идущую задачу — только с "
        "`swarm.thread_in_store`.\n\n"
        f"{_AUTH_NOTE}\n\nПодробно — docs/swarm.md."
    ),
    groups={
        **_TASK_GROUPS,
        "history": Group(
            "История", "История треда задачи, события и вывод песочницы."
        ),
    },
    routes=_SWARM_ROUTES,
)


# --- chat ---------------------------------------------------------------------

_CHAT_ERROR = _object(
    error=_object(
        message=_STR,
        type=_described(_STR, "`invalid_request_error`"),
        code=_described(
            _STR,
            "`invalid_request` | `invalid_api_key` | `not_found` | "
            "`conflict` | `server_error`",
        ),
    )
)
_CHAT_401 = _answer(
    _AUTH_MISSING,
    _CHAT_ERROR,
)
_RESPONSE = _object(
    id=_described(_STR, "`resp_<uuid>`"),
    object=_described(_STR, "`response`"),
    created_at=_INT,
    status=_described(
        _STR,
        "`in_progress` | `completed` | `incomplete` | `cancelled` | `failed`",
    ),
    model=_STR,
    previous_response_id=_STR,
    output=_described(
        {"type": "array", "items": _OBJ},
        "элементы `message` (проза), `reasoning`, `gc_tool_call` (карточка тула)",
    ),
    error=_OBJ,
    incomplete_details=_OBJ,
    metadata=_OBJ,
)
_CWD_META = _described(
    _object(
        cwd=_described(
            _STR,
            "Абсолютный каталог хода; нет — `chat.launch_root`, иначе "
            "каталог процесса",
        )
    ),
    "Метаданные запроса",
)
_FOLDER = _object(
    path=_described(_STR, "относительный путь, разделитель `/`"),
    entries={
        "type": "array",
        "items": _object(
            name=_STR,
            path=_STR,
            kind=_described(_STR, "`dir` | `file`"),
            text=_described(
                {"type": "boolean"}, "у файла: начало читается как UTF-8"
            ),
        ),
    },
)
_THREAD = (_path("thread_id", "Тред диалога Responses"),)
_REL_PATH = _query(
    "path", "Относительный путь внутри каталога; пусто — корень."
)
_MKDIR = {
    "required": True,
    **_json(
        _object(
            path=_described(_STR, "родительский каталог (пусто — корень)"),
            name=_described(_STR, "одно имя: без `/`, `\\` и ведущей точки"),
        ),
        {"path": "", "name": "notes"},
    ),
}


def _folder_routes(prefix: str, params: tuple, scope: str) -> dict:
    return {
        ("GET", prefix): RouteDoc(
            group="files",
            summary=f"Каталог {scope}",
            description=(
                "Один уровень каталога: сначала каталоги, потом файлы. "
                "Скрытые имена, `..`, абсолютный путь и симлинки наружу — 404."
            ),
            parameters=(*params, _REL_PATH),
            responses={
                "200": _answer("Каталог", _FOLDER),
                "401": _CHAT_401,
                "404": _answer("Нет пути (или треда)"),
            },
        ),
        ("POST", prefix): RouteDoc(
            group="files",
            summary=f"Создать каталог ({scope})",
            body=_MKDIR,
            parameters=params,
            responses={
                "200": _answer(
                    "Создан",
                    _object(path=_STR, kind=_STR),
                    {"path": "notes", "kind": "dir"},
                ),
                "400": _answer("Имя не годится"),
                "401": _CHAT_401,
                "404": _answer("Нет родительского каталога (или треда)"),
                "409": _answer("Имя занято или идёт ход этого треда"),
            },
        ),
        ("DELETE", prefix): RouteDoc(
            group="files",
            summary=f"Удалить файл или каталог ({scope})",
            description="Каталог — вместе с содержимым; корень не удаляется.",
            parameters=(*params, _REL_PATH),
            responses={
                "200": _answer("Удалено"),
                "401": _CHAT_401,
                "404": _answer("Нет пути (или треда)"),
                "409": _answer("Идёт ход этого треда"),
            },
        ),
        ("GET", prefix + "/download"): RouteDoc(
            group="files",
            summary=f"Скачать файл ({scope})",
            parameters=(*params, _REL_PATH),
            responses={
                "200": {
                    "description": "Байты файла (attachment)",
                    "content": {
                        "application/octet-stream": {
                            "schema": {"type": "string", "format": "binary"}
                        }
                    },
                },
                "401": _CHAT_401,
                "404": _answer("Нет файла"),
            },
        ),
    }


_CHAT_ROUTES: dict[tuple[str, str], RouteDoc] = {
    ("GET", "/"): RouteDoc(
        group="page",
        summary="Страница чата",
        description=(
            "HTML-страница чата. Токен на неё не нужен: его вводят на "
            "странице, дальше он уходит заголовком."
        ),
        responses={
            "200": {
                "description": "Страница",
                "content": {"text/html": {"schema": _STR}},
            }
        },
    ),
    ("GET", "/v1/models"): RouteDoc(
        group="models",
        summary="Модели",
        description=(
            "`GC-UA` — агент (тулы, память, субагенты); следующий id — "
            "модель из конфига агента: в Chat Completions запрос с ним идёт "
            "прямо в модель, без агента."
        ),
        responses={
            "200": _answer(
                "Список",
                _object(object=_STR, data={"type": "array", "items": _OBJ}),
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "GC-UA",
                            "object": "model",
                            "created": 0,
                            "owned_by": "gc-chat",
                        }
                    ],
                },
            )
        },
    ),
    ("POST", "/v1/responses"): RouteDoc(
        group="responses",
        summary="Ход агента (Responses)",
        description=(
            "Один запрос — один ход. Без `previous_response_id` — новый "
            'диалог (тред `system="responses"`), с ним — продолжение. Пока '
            "ход этого треда идёт, второй запрос — 409. `stream: true` — "
            "`text/event-stream` до терминального события, иначе запрос "
            "ждёт конца хода. `instructions`, `tools`, `temperature`, "
            "`max_output_tokens` принимаются и на ход не влияют."
        ),
        body={
            "required": True,
            **_json(
                {
                    "type": "object",
                    "required": ["model", "input"],
                    "properties": {
                        "model": _described(_STR, "`GC-UA`"),
                        "input": _described(
                            {
                                "oneOf": [
                                    _STR,
                                    {"type": "array", "items": _OBJ},
                                ]
                            },
                            "Строка или массив; в ход берётся только "
                            "последний элемент (текст user).",
                        ),
                        "stream": {"type": "boolean", "default": False},
                        "previous_response_id": _described(
                            _STR, "Продолжить диалог этого ответа"
                        ),
                        "metadata": _CWD_META,
                    },
                    "additionalProperties": True,
                },
                new={"model": "GC-UA", "input": "привет", "stream": False},
                follow_up={
                    "model": "GC-UA",
                    "input": "а теперь тесты",
                    "previous_response_id": "resp_…",
                },
                workdir={
                    "model": "GC-UA",
                    "input": "разберись в проекте",
                    "metadata": {"cwd": "/srv/project"},
                },
            ),
        },
        responses={
            "200": {
                "description": "Объект ответа (или SSE при `stream: true`)",
                **_json(_RESPONSE),
            },
            "400": _answer(_BAD_BODY, _CHAT_ERROR),
            "401": _CHAT_401,
            "409": _answer("У этого треда ещё идёт ход", _CHAT_ERROR),
            "500": _answer(
                "Ход без стрима упал: объект ответа со `status: failed`",
                _RESPONSE,
            ),
        },
    ),
    ("GET", "/v1/responses/{response_id}"): RouteDoc(
        group="responses",
        summary="Ответ по id",
        description="Сохранённый объект, в том числе `in_progress`.",
        parameters=(_path("response_id", "`resp_…`"),),
        responses={
            "200": _answer("Объект ответа", _RESPONSE),
            "401": _CHAT_401,
            "404": _answer("Нет такого ответа", _CHAT_ERROR),
        },
    ),
    ("POST", "/v1/responses/{response_id}/cancel"): RouteDoc(
        group="responses",
        summary="Отменить ход",
        description=(
            "Идущий ход — `cancelled` (с любого инстанса); после "
            "терминального статуса — сохранённый объект без изменений."
        ),
        parameters=(_path("response_id", "`resp_…`"),),
        responses={
            "200": _answer("Объект ответа", _RESPONSE),
            "401": _CHAT_401,
            "404": _answer("Нет такого ответа", _CHAT_ERROR),
        },
    ),
    ("POST", "/v1/chat/completions"): RouteDoc(
        group="completions",
        summary="Ход агента (Chat Completions)",
        description=(
            "OpenAI-совместимый: клиент каждый раз шлёт весь `messages`; "
            "история сеется во временный тред и после хода удаляется. "
            "Трейс (мысли, карточки тулов) — внутри `<think>…</think>`, "
            "затем ответ. `model: GC-UA` — агент; id модели из конфига — "
            "прямо в модель."
        ),
        body={
            "required": True,
            **_json(
                {
                    "type": "object",
                    "required": ["model", "messages"],
                    "properties": {
                        "model": _described(_STR, "`GC-UA`"),
                        "messages": {
                            "type": "array",
                            "items": _object(
                                role=_described(
                                    _STR, "`system` | `user` | `assistant`"
                                ),
                                content=_STR,
                            ),
                        },
                        "stream": {"type": "boolean", "default": False},
                        "metadata": _CWD_META,
                    },
                    "additionalProperties": True,
                },
                {
                    "model": "GC-UA",
                    "messages": [{"role": "user", "content": "привет"}],
                    "stream": False,
                },
            ),
        },
        responses={
            "200": _answer(
                "`chat.completion` (или чанки SSE при `stream: true`)", _OBJ
            ),
            "400": _answer(_BAD_BODY, _CHAT_ERROR),
            "401": _CHAT_401,
        },
    ),
    ("GET", "/v1/dialogs"): RouteDoc(
        group="dialogs",
        summary="Диалоги",
        description="Прошлые диалоги Responses, сначала недавно обновлённые.",
        responses={
            "200": _answer(
                "Список",
                {
                    "type": "array",
                    "items": _object(
                        thread_id=_STR,
                        latest_response_id=_STR,
                        cwd=_STR,
                        created_at=_INT,
                        updated_at=_INT,
                        status=_STR,
                        title=_STR,
                    ),
                },
            ),
            "401": _CHAT_401,
        },
    ),
    ("GET", "/v1/dialogs/{thread_id}"): RouteDoc(
        group="dialogs",
        summary="Ходы диалога",
        description="По цепочке `previous_response_id`, от первого к последнему.",
        parameters=_THREAD,
        responses={
            "200": _answer(
                "Ходы",
                {
                    "type": "array",
                    "items": _object(
                        response_id=_STR,
                        previous_response_id=_STR,
                        created_at=_INT,
                        status=_STR,
                        text=_described(_STR, "запрос пользователя"),
                        output={"type": "array", "items": _OBJ},
                    ),
                },
            ),
            "401": _CHAT_401,
            "404": _answer("Нет треда"),
        },
    ),
    **_folder_routes("/v1/workspace/artifacts", (), "корня нового хода"),
    **_folder_routes(
        "/v1/dialogs/{thread_id}/artifacts", _THREAD, "хода диалога"
    ),
}

CHAT = SurfaceDoc(
    title="GigaCode · Chat",
    description=(
        "Агент через OpenAI-совместимый API: Responses (диалог помнит "
        "сервер, продолжение — `previous_response_id`) и Chat Completions "
        "(клиент шлёт всю историю). Модель `GC-UA` — агент целиком: тулы, "
        "файлы каталога хода, память, субагенты.\n\n"
        "**Авторизация.** При заданном `auth_token` каждый `/v1/…` несёт "
        "`Authorization: Bearer <token>` (кнопка Authorize), иначе 401 "
        "`invalid_api_key`.\n\nПодробно — docs/chat.md и docs/responses.md."
    ),
    groups={
        "page": Group("Страница", "HTML-страница чата."),
        "models": Group("Модели", "Что можно указать в `model`."),
        "responses": Group("Responses", "Ход агента; диалог помнит сервер."),
        "completions": Group(
            "Chat Completions", "Ход агента; историю шлёт клиент."
        ),
        "dialogs": Group("Диалоги", "Прошлые диалоги Responses."),
        "files": Group(
            "Файлы", "Каталог хода: просмотр, скачивание, создание, удаление."
        ),
    },
    routes=_CHAT_ROUTES,
)

# --- A2A -----------------------------------------------------------------------
# Ручки — от a2a-sdk (протокол A2A 1.0): REST-привязка и JSON-RPC на /rpc.
# Поля — JSON протобуфов, camelCase; query-параметры — тоже camelCase.

_PART = _described(
    _object(
        text=_STR,
        url=_described(_STR, "файл по ссылке"),
        data=_described(_OBJ, "структурированные данные"),
        mediaType=_STR,
    ),
    "Часть сообщения: агент читает `text`",
)
_MESSAGE = _object(
    role=_described(_STR, "`ROLE_USER` | `ROLE_AGENT`"),
    messageId=_described(_STR, "уникальный id сообщения (задаёт клиент)"),
    contextId=_described(
        _STR, "диалог; нет — новый. Задачи одного контекста делят историю"
    ),
    taskId=_described(
        _STR, "только ответ задаче в `TASK_STATE_INPUT_REQUIRED`"
    ),
    parts={"type": "array", "items": _PART},
    metadata=_described(
        _object(
            cwd=_described(
                _STR,
                "каталог хода: абсолютный путь внутри `a2a.launch_root`; "
                "диалог его запоминает",
            )
        ),
        "метаданные",
    ),
)
_STATE = _described(
    _STR,
    "`TASK_STATE_SUBMITTED` (в очереди диалога) | `TASK_STATE_WORKING` | "
    "`TASK_STATE_INPUT_REQUIRED` (агент спросил) | `TASK_STATE_COMPLETED` | "
    "`TASK_STATE_FAILED` | `TASK_STATE_CANCELED`",
)
_TASK = _object(
    id=_STR,
    contextId=_STR,
    status=_object(
        state=_STATE,
        message=_described(_OBJ, "вопрос агента или текст отказа"),
        timestamp=_STR,
    ),
    artifacts=_described(
        {"type": "array", "items": _OBJ}, "ответ: артефакт `answer`"
    ),
    history={"type": "array", "items": _OBJ},
)
_TASK_EXAMPLE = {
    "id": _TASK_ID_EXAMPLE,
    "contextId": "426f1d3b-…",
    "status": {
        "state": "TASK_STATE_COMPLETED",
        "timestamp": "2026-10-22T12:33:11Z",
    },
    "artifacts": [{"name": "answer", "parts": [{"text": "14"}]}],
}
_SEND_BODY = {
    "required": True,
    **_json(
        _object(
            message=_MESSAGE,
            configuration=_object(
                returnImmediately=_described(
                    {"type": "boolean"},
                    "ответить сразу (`SUBMITTED`/`WORKING`), не дожидаясь хода",
                ),
                historyLength=_INT,
                acceptedOutputModes={"type": "array", "items": _STR},
                taskPushNotificationConfig=_described(
                    _OBJ,
                    "push по ходу задачи (нужен `a2a.push_allowed_hosts`)",
                ),
            ),
            metadata=_described(
                _OBJ, "метаданные запроса (`cwd` — как у сообщения)"
            ),
        ),
        new={
            "message": {
                "role": "ROLE_USER",
                "messageId": "m-1",
                "parts": [{"text": "Сколько будет 6+6?"}],
            }
        },
        same_dialog={
            "message": {
                "role": "ROLE_USER",
                "messageId": "m-2",
                "contextId": "426f1d3b-…",
                "parts": [{"text": "а теперь умножь на 2"}],
            }
        },
        answer={
            "message": {
                "role": "ROLE_USER",
                "messageId": "m-3",
                "taskId": "задача в INPUT_REQUIRED",
                "contextId": "её contextId",
                "parts": [{"text": "зелёный"}],
            }
        },
        background={
            "message": {
                "role": "ROLE_USER",
                "messageId": "m-4",
                "parts": [{"text": "напиши рассказ про маяк"}],
            },
            "configuration": {"returnImmediately": True},
        },
    ),
}
_A2A_403 = _answer(_AUTH_MISSING)
_A2A_TASK = (_path("id", "Id задачи"),)
_A2A_VERSION = _header("A2A-Version", "Версия протокола: `1.0`.")
_SSE = {
    "description": "Поток событий: `task`, `statusUpdate`, `artifactUpdate`",
    "content": {"text/event-stream": {"schema": _STR}},
}
_PUSH = _described(
    _object(id=_STR, url=_STR, token=_STR, authentication=_OBJ),
    "Настройка push; адрес — только под `a2a.push_allowed_hosts`",
)
_PUSH_OFF = (
    " Без `a2a.push_allowed_hosts` push выключен: карточка объявляет "
    "`pushNotifications: false`, ручки отвечают ошибкой."
)
_RPC_METHODS = (
    "SendMessage",
    "SendStreamingMessage",
    "GetTask",
    "ListTasks",
    "CancelTask",
    "SubscribeToTask",
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
    "GetExtendedAgentCard",
)

_A2A_ROUTES: dict[tuple[str, str], RouteDoc] = {
    ("GET", "/.well-known/agent-card.json"): RouteDoc(
        group="card",
        summary="Карточка агента",
        description=(
            "Описание агента для пира: имя, интерфейсы (JSON-RPC `…/rpc` и "
            "HTTP+JSON), возможности, схема авторизации. Без токена."
        ),
        responses={"200": _answer("Карточка", _OBJ)},
    ),
    ("GET", "/extendedAgentCard"): RouteDoc(
        group="card",
        summary="Расширенная карточка",
        description="Карточка для авторизованного пира (если объявлена).",
        responses={"200": _answer("Карточка", _OBJ), "403": _A2A_403},
    ),
    ("POST", "/message:send"): RouteDoc(
        group="messages",
        summary="Отправить сообщение",
        description=(
            "Без `taskId` — новая задача (один ход агента) в диалоге "
            "`contextId`. Диалог ведёт задачи по очереди: занят — задача "
            "`TASK_STATE_SUBMITTED` и стартует, когда он освободится. Вопрос "
            "модели — `TASK_STATE_INPUT_REQUIRED`: ответ — сообщение с "
            "`taskId` и `contextId` задачи, на любом инстансе. Сообщение в "
            "задачу, которая не ждёт ответа, — отказ. Без "
            "`returnImmediately` запрос ждёт конца хода."
        ),
        body=_SEND_BODY,
        parameters=(_A2A_VERSION,),
        responses={
            "200": _answer(
                "Задача (или сообщение)",
                _object(task=_TASK, message=_MESSAGE),
                {"task": _TASK_EXAMPLE},
            ),
            "400": _answer("Негодный запрос или задача не ждёт сообщения"),
            "403": _A2A_403,
            "404": _answer("Нет задачи `taskId`"),
        },
    ),
    ("POST", "/message:stream"): RouteDoc(
        group="messages",
        summary="Отправить сообщение (поток)",
        description="То же, что `message:send`, но события хода — потоком SSE.",
        body=_SEND_BODY,
        parameters=(_A2A_VERSION,),
        responses={
            "200": _SSE,
            "400": _answer("Негодный запрос"),
            "403": _A2A_403,
        },
    ),
    ("GET", "/tasks"): RouteDoc(
        group="tasks",
        summary="Список задач",
        description="Задачи из хранилища — одинаково на любом инстансе.",
        parameters=(
            _query("contextId", "Только задачи этого диалога."),
            _query("status", "Только в этом состоянии (`TASK_STATE_…`)."),
            _query("pageSize", "Размер страницы.", _INT),
            _query("pageToken", "Курсор следующей страницы."),
            _query("historyLength", "Сколько сообщений истории отдать.", _INT),
            _query(
                "statusTimestampAfter",
                "Только со статусом новее (RFC 3339).",
            ),
            _query(
                "includeArtifacts", "Отдавать артефакты.", {"type": "boolean"}
            ),
        ),
        responses={
            "200": _answer(
                "Страница задач",
                _object(
                    tasks={"type": "array", "items": _TASK},
                    nextPageToken=_STR,
                    pageSize=_INT,
                    totalSize=_INT,
                ),
            ),
            "403": _A2A_403,
        },
    ),
    ("GET", "/tasks/{id}"): RouteDoc(
        group="tasks",
        summary="Задача",
        parameters=(
            *_A2A_TASK,
            _query("historyLength", "Сколько сообщений истории отдать.", _INT),
        ),
        responses={
            "200": _answer("Задача", _TASK, _TASK_EXAMPLE),
            "403": _A2A_403,
            "404": _answer(_NO_TASK),
        },
    ),
    ("POST", "/tasks/{id}:cancel"): RouteDoc(
        group="tasks",
        summary="Отменить задачу",
        description=(
            "С любого инстанса: идущий ход останавливается, ожидание ответа "
            "и место в очереди диалога снимаются. Финальную задачу отменить "
            "нельзя."
        ),
        parameters=_A2A_TASK,
        responses={
            "200": _answer("Задача `TASK_STATE_CANCELED`", _TASK),
            "400": _answer("Задача уже финальна"),
            "403": _A2A_403,
            "404": _answer(_NO_TASK),
        },
    ),
    ("GET", "/tasks/{id}:subscribe"): RouteDoc(
        group="tasks",
        summary="Подписаться на задачу",
        description="События идущей задачи — на инстансе, который её ведёт.",
        parameters=_A2A_TASK,
        responses={"200": _SSE, "403": _A2A_403, "404": _answer(_NO_TASK)},
    ),
    ("POST", "/tasks/{id}:subscribe"): RouteDoc(
        group="tasks",
        summary="Подписаться на задачу (POST)",
        description="То же, что GET.",
        parameters=_A2A_TASK,
        responses={"200": _SSE, "403": _A2A_403, "404": _answer(_NO_TASK)},
    ),
    ("POST", "/tasks/{id}/pushNotificationConfigs"): RouteDoc(
        group="push",
        summary="Создать настройку push",
        description="Куда слать события задачи." + _PUSH_OFF,
        parameters=_A2A_TASK,
        body={"required": True, **_json(_PUSH)},
        responses={"200": _answer("Настройка", _PUSH), "403": _A2A_403},
    ),
    ("GET", "/tasks/{id}/pushNotificationConfigs"): RouteDoc(
        group="push",
        summary="Настройки push задачи",
        description=_PUSH_OFF.strip(),
        parameters=_A2A_TASK,
        responses={"200": _answer("Настройки", _OBJ), "403": _A2A_403},
    ),
    ("GET", "/tasks/{id}/pushNotificationConfigs/{push_id}"): RouteDoc(
        group="push",
        summary="Настройка push",
        parameters=(*_A2A_TASK, _path("push_id", "Id настройки")),
        responses={"200": _answer("Настройка", _PUSH), "403": _A2A_403},
    ),
    ("DELETE", "/tasks/{id}/pushNotificationConfigs/{push_id}"): RouteDoc(
        group="push",
        summary="Удалить настройку push",
        parameters=(*_A2A_TASK, _path("push_id", "Id настройки")),
        responses={"200": _answer("Удалено"), "403": _A2A_403},
    ),
    ("POST", "/rpc"): RouteDoc(
        group="rpc",
        summary="JSON-RPC 2.0",
        description=(
            "Те же операции одной ручкой: `method` — "
            + ", ".join(f"`{m}`" for m in _RPC_METHODS)
            + ". `params` — тело соответствующей REST-ручки (для "
            '`GetTask`/`CancelTask` — `{"id": …}`). Неизвестный метод — '
            "ошибка `-32601`."
        ),
        body={
            "required": True,
            **_json(
                {
                    "type": "object",
                    "required": ["jsonrpc", "method", "id"],
                    "properties": {
                        "jsonrpc": {"type": "string", "enum": ["2.0"]},
                        "id": {"oneOf": [_STR, _INT]},
                        "method": {
                            "type": "string",
                            "enum": list(_RPC_METHODS),
                        },
                        "params": _OBJ,
                    },
                },
                send_message={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "role": "ROLE_USER",
                            "messageId": "m-1",
                            "parts": [{"text": "привет"}],
                        }
                    },
                },
                get_task={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "GetTask",
                    "params": {"id": _TASK_ID_EXAMPLE},
                },
                cancel_task={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "CancelTask",
                    "params": {"id": _TASK_ID_EXAMPLE},
                },
            ),
        },
        parameters=(_A2A_VERSION,),
        responses={
            "200": _answer(
                "`result` или `error` JSON-RPC",
                _object(jsonrpc=_STR, id={}, result=_OBJ, error=_OBJ),
                {"jsonrpc": "2.0", "id": 1, "result": {"task": _TASK_EXAMPLE}},
            ),
            "403": _A2A_403,
        },
    ),
}

A2A = SurfaceDoc(
    title="GigaCode · A2A",
    description=(
        "Агент для других агентов по протоколу A2A 1.0 (a2a-sdk): "
        "REST-привязка и JSON-RPC (`/rpc`). Диалог — `contextId`, задача — "
        "один ход агента; задачи диалога идут по очереди, вопрос модели — "
        "`TASK_STATE_INPUT_REQUIRED`, ответ — на любом инстансе. Задачи — в "
        "хранилище тредов, одинаковые на любом инстансе.\n\n"
        "**Авторизация.** При заданном `auth_token` каждый запрос, кроме "
        "карточки, несёт `Authorization: Bearer <token>` (кнопка Authorize), "
        "иначе 403. Поля — JSON протобуфов A2A, camelCase.\n\n"
        "Подробно — docs/a2a.md и спецификация A2A."
    ),
    groups={
        "messages": Group(
            "Сообщения", "Новая задача, ответ на вопрос агента."
        ),
        "tasks": Group(
            "Задачи", "Статус, список, отмена, подписка на события."
        ),
        "push": Group("Push-уведомления", "Куда слать события задачи."),
        "card": Group("Карточка", "Описание агента для пира."),
        "rpc": Group("JSON-RPC", "Все операции одной ручкой."),
    },
    routes=_A2A_ROUTES,
)

#: Поверхности с документацией. ``disabled_routes`` — у всех, кроме A2A: его
#: карточка объявляет оба интерфейса целиком.
SURFACES: dict[str, SurfaceDoc] = {
    "webhook": WEBHOOK,
    "swarm": SWARM,
    "chat": CHAT,
    "a2a": A2A,
}

METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def unknown_disabled(surface: str, entries: list[str]) -> list[str]:
    """Записи ``disabled_routes``, которым нет ни группы, ни ручки."""
    from fnmatch import fnmatchcase

    doc = SURFACES[surface]
    unknown = []
    for entry in entries:
        if entry in doc.groups:
            continue
        method, path = split_route_entry(entry)
        if path is None or not any(
            (method is None or method == m) and fnmatchcase(p, path)
            for m, p in doc.routes
        ):
            unknown.append(entry)
    return unknown


def split_route_entry(entry: str) -> tuple[str | None, str | None]:
    """``"DELETE /v1/x"`` → (``DELETE``, ``/v1/x``); ``"/v1/x*"`` → (None,
    ``/v1/x*``); имя группы → (None, None)."""
    head, _, tail = entry.strip().partition(" ")
    if tail and head.upper() in METHODS and tail.strip().startswith("/"):
        return head.upper(), tail.strip()
    if entry.strip().startswith("/"):
        return None, entry.strip()
    return None, None


__all__ = [
    "A2A",
    "CHAT",
    "SURFACES",
    "SWARM",
    "WEBHOOK",
    "Group",
    "RouteDoc",
    "SurfaceDoc",
    "split_route_entry",
    "unknown_disabled",
]
