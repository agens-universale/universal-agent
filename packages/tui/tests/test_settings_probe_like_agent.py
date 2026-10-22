"""Проба «Подключения» ходит к серверу так же, как агент.

Живой случай: /settings → «Подключение» писало «✗ токен отклонён (401)», а агент
с тем же конфигом работал — за корпоративным шлюзом. Проба шла не тем путём,
что агент: без дополнительных заголовков `backend.chat_completion_headers`, с
заглушкой вместо модели основной записи и с общим токеном вместо токена записи.

Сервер здесь настоящий (127.0.0.1): заголовки собирает транспорт, и фейк
транспорта не увидел бы, что именно ушло по сети."""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_support import open_settings_section, wait_until

_GOOD = "Bearer good-token"


class _Gateway:
    """Шлюз со строгими правилами — как корпоративный перед моделью.

    POST в chat/completions пропускает, только если совпали токен, служебный
    заголовок (если задан) и модель из списка доступа; иначе 401 — шлюз не
    различает, ЧТО именно не так. Справочников моделей у него нет (404 без
    JSON), поэтому вердикт проба выносит по самому chat/completions."""

    def __init__(self, *, header=None, models=None):
        self.header = header
        self.models = models
        self.posts: list[dict] = []
        gateway = self

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                self._send(404, b"Cannot GET", "text/html")

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                gateway.posts.append(
                    {"headers": dict(self.headers), "model": body.get("model")}
                )
                if gateway.admits(self.headers, body.get("model")):
                    self._send(200, b'{"choices": []}', "application/json")
                else:
                    self._send(401, b'{"error": "denied"}', "application/json")

            def _send(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self._srv = HTTPServer(("127.0.0.1", 0), _H)
        self.url = (
            f"http://127.0.0.1:{self._srv.server_port}/v1/chat/completions"
        )

    def admits(self, headers, model) -> bool:
        if headers.get("Authorization") != _GOOD:
            return False
        if self.header is not None:
            name, value = self.header
            if headers.get(name) != value:
                return False
        return self.models is None or model in self.models

    def __enter__(self):
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()


@asynccontextmanager
async def _connection_section(tmp_path, yaml_text):
    """Секция «Подключение» на настоящем config.yml с настоящей пробой (без
    подмен `probe_fn`): проверяется ровно то, что уходит по сети."""
    from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    from gc_testkit.tui_support import tui_settings

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = app_mod.TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()
    async with app.run_test() as pilot:
        scr = SettingsScreen(
            settings=app.base_settings(),
            config_path=str(cfg_file),
            on_apply=app.reload_config,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await wait_until(
            pilot,
            lambda: scr._draft_checked is not None,
            "автопроба секции «Подключение» осела",
            steps=400,
        )
        yield pilot, scr


def _verdict(scr) -> str:
    from gc_tui.services.probe_presentation import connection_line_text

    return connection_line_text(scr._draft_checked).plain


async def test_probe_sends_the_extra_gateway_header_like_the_agent(tmp_path):
    with _Gateway(header=("X-Gateway-Key", "corp-key")) as gw:
        yaml_text = (
            "backend:\n"
            "  api_mode: chat_completions\n"
            f"  completions_url: {gw.url}\n"
            "  chat_completion_headers:\n"
            f"    Authorization: {_GOOD}\n"
            "    X-Gateway-Key: corp-key\n"
            "agent:\n"
            "  model: corp-model\n"
            "  x_cl_id: cl-42\n"
        )
        async with _connection_section(tmp_path, yaml_text) as (_p, scr):
            assert scr._draft_checked.auth_state == "accepted", _verdict(scr)
            assert "отклонён" not in _verdict(scr)
            sent = gw.posts[-1]["headers"]
            assert sent.get("X-Gateway-Key") == "corp-key"
            assert sent.get("x_cl_id") == "cl-42"
            # Проба не притворяется сессией агента.
            assert "x-session-id" not in {k.lower() for k in sent}


async def test_probe_sends_the_main_registry_model_not_a_placeholder(
    tmp_path,
):
    """Шлюз с доступом по модели отвечает 401 на неизвестную модель. Плоского
    `agent.model` нет (шаблонная заглушка) — основная модель живёт в реестре."""
    with _Gateway(models={"corp/main-model"}) as gw:
        yaml_text = (
            "backend:\n"
            "  api_mode: chat_completions\n"
            f"  completions_url: {gw.url}\n"
            "  chat_completion_headers:\n"
            f"    Authorization: {_GOOD}\n"
            "agent:\n"
            '  model: "<ВПИШИТЕ_МОДЕЛЬ>"\n'
            "models:\n"
            "  main:\n"
            "    model: corp/main-model\n"
            "slots:\n"
            "  default: main\n"
        )
        async with _connection_section(tmp_path, yaml_text) as (_p, scr):
            assert scr._draft_checked.auth_state == "accepted", _verdict(scr)
            assert {p["model"] for p in gw.posts} == {"corp/main-model"}


async def test_probe_checks_the_main_entry_own_token_not_the_shared_one(
    tmp_path,
):
    """Основная модель — запись реестра со СВОИМ подключением, общий
    `backend.*` держит устаревший токен. Агент ходит токеном записи и работает;
    нетронутая форма проверяет то, чем пойдёт агент, а не врёт «отклонён»."""
    with _Gateway() as gw:
        yaml_text = (
            "backend:\n"
            "  api_mode: chat_completions\n"
            f"  completions_url: {gw.url}\n"
            "  chat_completion_headers:\n"
            "    Authorization: Bearer stale-shared\n"
            "agent:\n"
            "  model: corp-model\n"
            "models:\n"
            "  main:\n"
            "    model: corp/main-model\n"
            f"    url: {gw.url}\n"
            f"    token: {_GOOD}\n"
            "slots:\n"
            "  default: main\n"
        )
        async with _connection_section(tmp_path, yaml_text) as (_p, scr):
            assert "отклонён" not in _verdict(scr)
            assert scr._draft_checked.auth_state == "accepted", _verdict(scr)
            assert gw.posts[-1]["model"] == "corp/main-model"
            # Принят токен записи, а не поля: в поле общего токена его форму
            # не переносим и черновик не пачкаем.
            assert scr._draft_checked.accepted_auth is None
            assert scr._connection_editor().peek()[2] == ""
            assert not scr._connection_editor().is_dirty()


async def test_typed_token_takes_priority_over_the_main_entry(tmp_path):
    """Напечатанное в форме проверяется как есть: человек спрашивает про
    введённый токен, и подменять его токеном записи нельзя."""
    from textual.widgets import Input

    with _Gateway() as gw:
        yaml_text = (
            "backend:\n"
            "  api_mode: chat_completions\n"
            f"  completions_url: {gw.url}\n"
            "  chat_completion_headers:\n"
            "    Authorization: Bearer stale-shared\n"
            "models:\n"
            "  main:\n"
            "    model: corp/main-model\n"
            f"    url: {gw.url}\n"
            f"    token: {_GOOD}\n"
            "slots:\n"
            "  default: main\n"
        )
        async with _connection_section(tmp_path, yaml_text) as (pilot, scr):
            field = scr._connection_editor().query_one("#conn-token", Input)
            field.value = "Bearer typed-wrong"
            await pilot.pause()  # правка поля сбрасывает прежний вердикт
            scr._draft_checked = None
            scr._start_draft_probe(scr._view, manual=True)
            await wait_until(
                pilot,
                lambda: scr._draft_checked is not None,
                "ручная проба осела",
                steps=400,
            )
            assert scr._draft_checked.auth_state == "rejected", _verdict(scr)
            assert gw.posts[-1]["headers"]["Authorization"] in (
                "Bearer typed-wrong",
                "typed-wrong",
            )


async def test_hub_glyph_follows_the_main_entry_own_connection(tmp_path):
    """Хаб судит тем же маршрутом, что секция: глиф не краснеет на общем
    токене, которым основная модель не ходит."""
    from gc_testkit.tui_support import settings_section_index

    with _Gateway() as gw:
        yaml_text = (
            "backend:\n"
            "  api_mode: chat_completions\n"
            f"  completions_url: {gw.url}\n"
            "  chat_completion_headers:\n"
            "    Authorization: Bearer stale-shared\n"
            "models:\n"
            "  main:\n"
            "    model: corp/main-model\n"
            f"    url: {gw.url}\n"
            f"    token: {_GOOD}\n"
            "slots:\n"
            "  default: main\n"
        )
        async with _connection_section(tmp_path, yaml_text) as (pilot, scr):
            await wait_until(
                pilot,
                lambda: (
                    scr._probe_config is not None and not scr._probe_in_flight
                ),
                "проба хаба осела",
                steps=400,
            )
            glyph = scr._menu_glyph(settings_section_index("Подключение"))
            assert glyph[0] == "✓"


# ── выбор цели пробы: чистая функция, без сети ──


def _form(endpoint="http://shared/v1/chat/completions", auth="Bearer typed"):
    from gc_tui.services.settings_probe import ProbeTarget

    return ProbeTarget(endpoint, auth, True, "", None)


def _route(own_endpoint=None):
    from gc_tui.services.settings_probe import AgentRoute, ProbeTarget

    own = None
    if own_endpoint is not None:
        own = ProbeTarget(
            own_endpoint,
            "Bearer own",
            "/own.pem",
            "corp/main-model",
            "http://own-proxy:3128",
            {"x_cl_id": "c", "X-Own": "1"},
            entry_own=True,
        )
    return AgentRoute(
        model="corp/main-model",
        headers={"x_cl_id": "c", "X-Own": "1"},
        shared_headers={"x_cl_id": "c"},
        own=own,
    )


def test_target_inheriting_main_checks_the_form_with_agent_model():
    from gc_tui.services.settings_probe import connection_probe_target

    target = connection_probe_target(_route(), _form(), form_edited=True)
    assert (target.endpoint, target.auth) == (_form().endpoint, "Bearer typed")
    assert target.model == "corp/main-model"
    assert target.headers == {"x_cl_id": "c", "X-Own": "1"}
    assert not target.entry_own


def test_target_untouched_form_checks_the_entry_own_connection():
    from gc_tui.services.settings_probe import connection_probe_target

    route = _route("http://own/v1/chat/completions")
    target = connection_probe_target(route, _form(), form_edited=False)
    assert target == route.own


def test_target_edited_form_on_another_address_checks_only_the_token():
    """Тронутая форма с основной записью на ДРУГОМ адресе: модель записи туда
    не ходит — проба идёт с заглушкой и общими заголовками."""
    from gc_tui.services.settings_probe import connection_probe_target

    route = _route("http://own/v1/chat/completions")
    target = connection_probe_target(route, _form(), form_edited=True)
    assert target.auth == "Bearer typed"
    assert target.model == ""
    assert target.headers == {"x_cl_id": "c"}
    assert not target.entry_own


def test_target_edited_form_on_the_entry_address_keeps_the_model():
    from gc_tui.services.settings_probe import connection_probe_target

    url = "http://own/v1/chat/completions"
    target = connection_probe_target(
        _route(url), _form(endpoint=url), form_edited=True
    )
    assert (target.auth, target.model) == ("Bearer typed", "corp/main-model")
    assert not target.entry_own


def test_route_without_own_token_inherits_the_shared_pair():
    """Запись без своего токена наследует пару целиком — проба идёт по форме."""
    from gc_core.model_router import ResolvedModel
    from gc_testkit.settings import make_settings
    from gc_tui.services.settings_probe import agent_route

    settings = make_settings({"backend.api_mode": "chat_completions"})
    resolved = ResolvedModel(
        alias="main",
        model="corp/main-model",
        url="http://shared/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )
    route = agent_route(settings, resolved)
    assert route.model == "corp/main-model"
    assert route.own is None


def test_route_own_token_from_model_headers_wins_like_the_agent():
    from gc_core.model_router import ResolvedModel
    from gc_testkit.settings import make_settings
    from gc_tui.services.settings_probe import agent_route

    settings = make_settings(
        {"backend.api_mode": "chat_completions", "backend.proxy": "http://p"}
    )
    resolved = ResolvedModel(
        alias="main",
        model="corp/main-model",
        url="http://own/v1/chat/completions",
        authorization="Bearer entry",
        backend_overrides={
            "use_ssl": False,
            "chat_completion_headers": {"Authorization": "Bearer header"},
        },
        tariff=None,
        source="test",
    )
    own = agent_route(settings, resolved).own
    assert own is not None and own.entry_own
    assert own.auth == "Bearer header"
    assert own.verify is False
    assert own.proxy == "http://p"
    assert own.endpoint == "http://own/v1/chat/completions"


def test_route_in_responses_mode_has_no_own_connection():
    """В responses-режиме от записи действует только id модели (§4)."""
    from gc_core.model_router import ResolvedModel
    from gc_testkit.settings import make_settings
    from gc_tui.services.settings_probe import agent_route

    resolved = ResolvedModel(
        alias="main",
        model="corp/main-model",
        url="http://own/v1/chat/completions",
        authorization="Bearer entry",
        backend_overrides={},
        tariff=None,
        source="test",
    )
    route = agent_route(
        make_settings({"backend.api_mode": "responses"}), resolved
    )
    assert route.own is None
    assert route.model == "corp/main-model"


def test_target_edited_form_with_trailing_slash_keeps_the_model():
    """Косметика адреса (хвостовой `/`) — тот же адрес: модель записи не
    подменяется заглушкой."""
    from gc_tui.services.settings_probe import connection_probe_target

    url = "http://own/v1/chat/completions"
    target = connection_probe_target(
        _route(url), _form(endpoint=url + "/"), form_edited=True
    )
    assert (target.auth, target.model) == ("Bearer typed", "corp/main-model")
    assert target.headers == {"x_cl_id": "c", "X-Own": "1"}


@pytest.mark.parametrize(
    ("agent_model", "expected"),
    [("corp/raw-model", "corp/raw-model"), (None, "")],
    ids=["raw-model", "sentinel"],
)
def test_route_without_resolve_falls_back_to_the_agent_model(
    agent_model, expected
):
    """Резолв `ModelRouter` не удался — проба шлёт `agent.model`, как до
    правки, а не заглушку: гейтвей с ACL по модели заглушку отклонит.
    Плейсхолдер шаблона именем модели не считается."""
    from gc_core.onboarding import MODEL_SENTINEL
    from gc_testkit.settings import make_settings
    from gc_tui.services.settings_probe import agent_route

    model = MODEL_SENTINEL if agent_model is None else agent_model
    settings = make_settings(
        {"backend.api_mode": "chat_completions", "agent.model": model}
    )
    route = agent_route(settings, None)
    assert route.model == expected
    assert route.own is None
