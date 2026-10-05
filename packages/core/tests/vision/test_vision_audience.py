"""«Кто смотрит» изображения запроса (спека изображений §9.1).

Роутер настоящий, но без конфига с диска: снимок — словарь `models`/`slots`/
`assign`, как его читает `ModelRouter`. Так проверяется действующее правило
резолва «глаз» (привязка / активный уровень / предохранитель), а не его копия
в двойнике.
"""

from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter, ResolvedModel
from gc_testkit.settings import make_settings
from gc_core.vision.audience import Audience, compute_audience
from gc_core.vision.rejects import request_url
from gc_core.vision.rejects import ImageRejects, mark_blind
from gc_core.vision.audience import route_id
from gc_core.vision.route import VisionRouteKey
from vision_fakes import confirm

_GLM_URL = "http://ecs3:6003/v1/chat/completions"
_QWEN_URL = "https://openrouter.ai/api/v1/chat/completions"
_FALLBACK_URL = "http://fallback/v1/chat/completions"


def _router(
    assign: dict | None = None, *, backend: dict | None = None
) -> ModelRouter:
    """`backend` — явные ключи `models.<alias>.backend` по алиасам, как их
    проецирует конфиг (`backend_overrides`)."""
    backend = backend or {}

    def entry(model, url, alias):
        out = {"model": model, "url": url, "token": ""}
        if alias in backend:
            out["backend"] = backend[alias]
        return out

    snapshot = AppConfig.model_validate(
        {
            "models": {
                "glm": entry("glm-5.1", _GLM_URL, "glm"),
                "qwen": entry("qwen/qwen3.6-35b-a3b", _QWEN_URL, "qwen"),
            },
            "slots": {"default": "glm"},
            "assign": assign or {},
        }
    )
    return ModelRouter(snapshot, level_provider=lambda: "default")


def _key(url, model):
    return VisionRouteKey(url, model, "test")


def _audience(
    router,
    rejects,
    *,
    api_mode="chat_completions",
    tool_in_set=True,
    request_resolved=None,
    config=None,
) -> Audience:
    request = request_resolved or router.resolve("main")
    resolved_models = [request]
    try:
        resolved_models.append(router.resolve("vision"))
    except (ValueError, RuntimeError):
        pass
    routes = {}
    for resolved in resolved_models:
        key = _key(resolved.url or _FALLBACK_URL, resolved.model)
        confirm(key)
        routes[route_id(resolved)] = key
    return compute_audience(
        router,
        request_resolved=request_resolved or router.resolve("main"),
        api_mode=api_mode,
        tool_in_set=tool_in_set,
        settings=make_settings(config),
        routes=routes,
    )


def test_a_seeing_request_model_looks_natively():
    router = _router()
    audience = _audience(router, ImageRejects())

    assert audience.native == router.resolve("main")
    # «Глаза» без привязки — та же активная модель: запас на случай её пометки.
    assert audience.eyes == router.resolve("main")
    assert audience.tool_visible is True
    assert audience.nobody is False


def test_a_marked_request_model_without_eyes_leaves_nobody():
    """Слепая модель хода, «глаза» не назначены: неназначенные «глаза» — та же
    помеченная модель, и смотреть некому."""
    router = _router()
    rejects = ImageRejects()
    mark_blind(_key(_GLM_URL, "glm-5.1"))

    audience = _audience(router, rejects)

    assert audience == Audience(
        native=None,
        eyes=None,
        tool_visible=False,
        probe_reason="image_unsupported",
    )
    assert audience.nobody is True


def test_a_marked_request_model_hands_over_to_the_assigned_eyes():
    router = _router({"vision": "qwen"})
    rejects = ImageRejects()
    mark_blind(_key(_GLM_URL, "glm-5.1"))

    audience = _audience(router, rejects)

    assert audience.native is None
    assert audience.eyes.alias == "qwen"
    assert audience.tool_visible is True


def test_marked_eyes_do_not_hide_a_seeing_request_model():
    router = _router({"vision": "qwen"})
    rejects = ImageRejects()
    mark_blind(_key(_QWEN_URL, "qwen/qwen3.6-35b-a3b"))

    audience = _audience(router, rejects)

    assert audience.native.alias == "glm"
    assert audience.eyes is None
    assert audience.tool_visible is True
    assert audience.nobody is False


def test_responses_mode_never_shows_images_natively():
    """В `responses` картинка осталась бы в цепочке `previous_response_id`
    (спека §20.5): основная модель получает записки, смотрят «глаза»."""
    router = _router()
    audience = _audience(router, ImageRejects(), api_mode="responses")

    assert audience.native is None
    assert audience.eyes == router.resolve("main")
    assert audience.tool_visible is True


def test_the_tool_is_invisible_when_it_is_not_in_the_set():
    """`stop_tools`, список тулов субагента: записки не отправляют к тулу,
    которого у модели нет, даже если смотреть есть кому."""
    audience = _audience(_router(), ImageRejects(), tool_in_set=False)

    assert audience.native is not None
    assert audience.tool_visible is False


def test_eyes_whose_alias_is_open_in_the_breaker_are_absent():
    """Алиас «глаз» уже признан недоступным в этой сессии: резолв отказывает
    мгновенно (`AliasCircuitOpenError`), и на «глаза» рассчитывать нельзя."""
    router = _router({"vision": "qwen"})
    router.breaker.trip("qwen", "503")
    rejects = ImageRejects()
    mark_blind(_key(_GLM_URL, "glm-5.1"))

    audience = _audience(router, rejects)

    assert audience.eyes is None
    assert audience.nobody is True


def test_an_unresolvable_eyes_level_does_not_break_the_request():
    """Решение считается на КАЖДЫЙ запрос и не бросает: две модели без слотов
    (валидатор такой конфиг не пропустит, но двойник или ручная сборка могут) —
    резолв «глаз» падает `ValueError`, запрос уходит с `eyes=None`."""
    # Мимо валидации схемы — как двойник или ручная сборка.
    from gc_core.config_modules.model_registry import ModelEntry

    snapshot = AppConfig().model_copy(
        update={
            "models": {
                "a": ModelEntry.model_validate({}),
                "b": ModelEntry.model_validate({}),
            }
        }
    )
    router = ModelRouter(snapshot, level_provider=lambda: "default")
    request = ResolvedModel(
        alias="a",
        model="a",
        url=_GLM_URL,
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )

    audience = _audience(router, ImageRejects(), request_resolved=request)

    assert audience.native == request
    assert audience.eyes is None
    assert audience.tool_visible is True


def test_the_key_uses_the_fallback_url_when_the_model_has_none():
    """Запись без своего адреса и без общего `completions_url` в снимке: запрос
    уходит на запасной адрес воронки — по нему же ставится и ищется пометка."""
    router = ModelRouter(
        AppConfig.model_validate(
            {"models": {"bare": {}}, "slots": {"default": "bare"}}
        ),
        level_provider=lambda: "default",
    )
    resolved = router.resolve("main")
    rejects = ImageRejects()
    mark_blind(_key(_FALLBACK_URL, "bare"))

    # Схема даёт пустую строку (общий `completions_url` по умолчанию ""), не None.
    assert not resolved.url
    assert request_url(resolved, _FALLBACK_URL) == _FALLBACK_URL
    assert _audience(router, rejects).nobody is True


def test_request_url_prefers_the_model_address():
    resolved = ResolvedModel(
        alias="glm",
        model="glm-5.1",
        url=_GLM_URL,
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )
    assert request_url(resolved, _FALLBACK_URL) == _GLM_URL


def test_legacy_request_model_is_keyed_by_its_model_id():
    """Легаси (пустой реестр): алиаса нет, ключ — адрес и id модели."""
    router = ModelRouter(
        make_settings({"model": "legacy-model", "completions_url": _GLM_URL}),
        level_provider=lambda: "default",
    )
    rejects = ImageRejects()
    mark_blind(_key(_GLM_URL, "legacy-model"))

    audience = _audience(router, rejects)

    assert audience.nobody is True


# --- `backend.vision: false`: модель заявлена слепой в конфиге -----------------
# Прокси, который молча выбрасывает картинку (HTTP 200, «не вижу»), отказа не
# даёт — узнать по факту нечем. Флаг в `models.<alias>.backend` (или в общей
# секции `backend`) говорит это заранее; смысл имеет только `false`.


def test_a_model_declared_text_only_never_gets_images_natively():
    """Слепая по флагу модель хода без назначенных «глаз»: неназначенные
    «глаза» — та же модель, смотреть некому."""
    router = _router(backend={"glm": {"vision": False}})

    audience = _audience(router, ImageRejects())

    assert audience == Audience(native=None, eyes=None, tool_visible=False)


def test_a_model_declared_text_only_hands_over_to_the_assigned_eyes():
    router = _router({"vision": "qwen"}, backend={"glm": {"vision": False}})

    audience = _audience(router, ImageRejects())

    assert audience.native is None
    assert audience.eyes.alias == "qwen"
    assert audience.tool_visible is True


def test_eyes_declared_text_only_are_absent():
    router = _router({"vision": "qwen"}, backend={"qwen": {"vision": False}})

    audience = _audience(router, ImageRejects())

    assert audience.native.alias == "glm"
    assert audience.eyes is None


def test_the_general_backend_flag_applies_to_every_model():
    """`backend.vision: false` в общей секции — плоский ключ `backend_vision`:
    слепы все модели, у которых нет своего значения."""
    router = _router({"vision": "qwen"})

    audience = _audience(
        router, ImageRejects(), config={"backend_vision": False}
    )

    assert audience.nobody is True


def test_a_model_value_beats_the_general_flag():
    router = _router({"vision": "qwen"}, backend={"qwen": {"vision": True}})

    audience = _audience(
        router, ImageRejects(), config={"backend_vision": False}
    )

    assert audience.native is None
    assert audience.eyes.alias == "qwen"


def test_vision_true_changes_nothing():
    """`true` — то же, что не задано: зрение узнаётся по факту, пометка отказа
    по-прежнему снимает модель."""
    router = _router(backend={"glm": {"vision": True}})
    rejects = ImageRejects()

    assert _audience(router, rejects).native.alias == "glm"

    mark_blind(_key(_GLM_URL, "glm-5.1"))
    assert _audience(router, rejects).nobody is True


def test_unknown_candidate_offers_tool_without_admitting_pixels():
    router = _router()
    audience = compute_audience(
        router,
        request_resolved=router.resolve("main"),
        api_mode="chat_completions",
        tool_in_set=True,
        settings=make_settings(),
    )
    assert audience.native is None and audience.eyes is None
    assert audience.tool_visible


def test_explicit_true_still_requires_confirmation():
    router = _router(backend={"glm": {"vision": True}})
    audience = compute_audience(
        router,
        request_resolved=router.resolve("main"),
        api_mode="chat_completions",
        tool_in_set=True,
        settings=make_settings(),
    )
    assert audience.native is None and audience.tool_visible
