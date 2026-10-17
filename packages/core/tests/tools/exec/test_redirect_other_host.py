"""Редирект на другой хост в manual и auto не выполняется: модель получает
адрес и запрашивает его отдельно — новый запрос проходит вопрос или судью.
Переходы в пределах хоста и весь edit — как раньше."""

import inspect
import json

import httpx
import pytest
from gc_core.execution.state import SharedStateData
from gc_core.message_policy.permission_denials import (
    OTHER_HOST_REDIRECT_TYPE,
)
from gc_core.security.permission_mode import follows_cross_host_redirects
from gc_core.security.permission_store import PermissionStore
from gc_core.security.url_policy import validate_redirect_hop
from gc_core.tools.exec.download_file import DownloadFile
from gc_core.tools.exec.fetch_url import FetchUrl
from gc_core.tools.exec.simple_fetch_url import SimpleFetchUrl
from gc_testkit.helpers.tool_ctx import CallableTool, local_ctx
from gc_testkit.settings import fake_ctx

START = "https://a.example/start"
OTHER = "https://b.example/page"
SCHEMES = ["http", "https"]


def _store(mode):
    shared = SharedStateData(permission_mode=mode)
    return PermissionStore(shared.confirm_rules, shared=shared)


def _server(monkeypatch, location, *targets):
    """Сервер: стартовый адрес отвечает 302 на `location`, остальные — 200.
    Возвращает список посещённых адресов."""
    visited = []

    def dispatch(request):
        url = str(request.url)
        visited.append(url)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(
            200, text="page", headers={"Content-Type": "text/plain"}
        )

    def factory(**_kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(dispatch))

    for target in targets:
        module_globals = inspect.unwrap(target).__globals__
        if "get_async_client" in module_globals:
            monkeypatch.setitem(module_globals, "get_async_client", factory)
    return visited


def _tool(cls, mode, root=None):
    """Тул с режимом сессии; человек на любой вопрос отвечает «да, разово»."""
    config = {}
    if root is not None:
        config = {
            "launch_root": str(root),
            "security.download": {
                "download_dir": "downloads",
                "max_bytes": 1_000_000,
            },
        }
    ctx = local_ctx(
        config,
        permission_store=_store(mode),
        is_tui=True,
        tui_confirm=lambda **kwargs: "allow_once",
    )
    return CallableTool(cls(ctx=ctx))


@pytest.mark.parametrize(
    "mode, follows",
    [("edit", True), ("plan", True), ("manual", False), ("auto", False)],
)
def test_mode_decides_whether_a_redirect_may_leave_the_host(mode, follows):
    ctx = fake_ctx(permission_store=_store(mode))
    assert follows_cross_host_redirects(ctx) is follows


def test_hop_decision_checks_the_policy_first_and_the_host_second():
    hop = validate_redirect_hop(
        START, "http://a.example/next", SCHEMES, [], same_host_only=True
    )
    assert hop.allowed is True  # тот же хост, смена схемы — переход идёт
    other = validate_redirect_hop(
        START, OTHER, SCHEMES, [], same_host_only=True
    )
    assert other.allowed is False
    assert other.error_type == OTHER_HOST_REDIRECT_TYPE
    assert OTHER in other.reason
    assert validate_redirect_hop(
        START, OTHER, SCHEMES, [], same_host_only=False
    ).allowed
    blocked = validate_redirect_hop(
        START, OTHER, SCHEMES, ["b.example"], same_host_only=True
    )
    assert blocked.error_type == "url_blocked_by_policy"


@pytest.mark.parametrize("cls", [FetchUrl, SimpleFetchUrl])
def test_manual_returns_a_redirect_to_another_host_to_the_model(
    monkeypatch, cls
):
    visited = _server(monkeypatch, OTHER, cls.acall)
    out = json.loads(_tool(cls, "manual")(url=START))
    assert out["ok"] is False
    assert out["error"]["type"] == OTHER_HOST_REDIRECT_TYPE
    assert OTHER in out["error"]["reason"]
    assert visited == [START]


def test_manual_download_does_not_follow_a_redirect_to_another_host(
    monkeypatch, tmp_path
):
    visited = _server(
        monkeypatch, OTHER, DownloadFile.acall, DownloadFile._http_get_bytes
    )
    tool = _tool(DownloadFile, "manual", tmp_path)
    out = json.loads(tool(url=START, filename="a.pdf"))
    assert out["error"]["type"] == OTHER_HOST_REDIRECT_TYPE
    assert OTHER in out["error"]["reason"]
    assert visited == [START]
    assert not (tmp_path / "downloads" / "a.pdf").exists()


def test_manual_follows_a_redirect_within_the_same_host(monkeypatch):
    visited = _server(
        monkeypatch, "https://a.example/final", SimpleFetchUrl.acall
    )
    out = json.loads(
        _tool(SimpleFetchUrl, "manual")(url="http://a.example/start")
    )
    assert out["ok"] is True
    assert visited == ["http://a.example/start", "https://a.example/final"]


def test_edit_follows_a_redirect_to_another_host_as_before(monkeypatch):
    visited = _server(monkeypatch, OTHER, SimpleFetchUrl.acall)
    out = json.loads(_tool(SimpleFetchUrl, "edit")(url=START))
    assert out["ok"] is True
    assert visited == [START, OTHER]


def test_auto_returns_a_redirect_to_another_host_to_the_model(monkeypatch):
    """auto на уровне тула: судья пропустил названный адрес, переход на
    другой хост не выполнен — модель запросит его отдельно, и этот запрос
    снова увидит судья."""
    from types import SimpleNamespace

    from gc_core.security import action_judge_step, permission_mode
    from gc_core.security.call_scope import call_scope
    from gc_core.security.permission_mode import Mode

    monkeypatch.setattr(
        permission_mode,
        "AVAILABLE_MODES",
        (Mode.EDIT, Mode.MANUAL, Mode.PLAN, Mode.AUTO),
    )
    judged = []

    async def judge(_ctx, _state, messages):
        judged.append(messages[-1]["content"])
        return json.dumps(
            {
                "reason": "",
                "crosses_boundary": False,
                "risky": False,
                "authorized": False,
            }
        )

    monkeypatch.setattr(action_judge_step, "_arequest_verdict_text", judge)
    monkeypatch.setattr(
        action_judge_step, "judge_channel_configured", lambda ctx: True
    )
    visited = _server(monkeypatch, OTHER, SimpleFetchUrl.acall)
    ctx = local_ctx(
        {}, permission_store=_store("auto"), logger=None, tools_data={}
    )
    state = SimpleNamespace(
        messages=[{"role": "user", "content": "открой страницу"}],
        user_intent_log=[],
        current_user_intent=[],
        shared=SharedStateData(),
        parent_state_id=None,
        judge_failures_in_a_row=0,
        judge_stop_reason=None,
    )
    with call_scope(
        state=state, call_id="c1", tool_name="exec_fetch_url"
    ) as scope:
        scope.arguments = {"url": START}
        out = json.loads(CallableTool(SimpleFetchUrl(ctx=ctx))(url=START))
    assert out["error"]["type"] == OTHER_HOST_REDIRECT_TYPE
    assert visited == [START]
    assert len(judged) == 1  # судья видел только названный адрес
    assert OTHER not in judged[0]
