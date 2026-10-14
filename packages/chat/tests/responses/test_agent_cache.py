"""Живые агенты Responses не копятся: сверх потолка закрывается самый давний простой."""

from gc_chat.responses import facade as facade_mod
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.index_store import ResponseIndex
from gc_testkit.settings import make_settings


class _Agent:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _facade(tmp_path, made: list[_Agent]) -> ResponsesFacade:
    async def factory(settings, **kwargs):
        made.append(_Agent())
        return made[-1]

    return ResponsesFacade(
        settings=make_settings(**{"agent.model": "m"}),
        agent_factory=factory,
        index=ResponseIndex.for_threads_dir(str(tmp_path)),
    )


async def test_idle_agents_evicted_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(facade_mod, "MAX_LIVE_AGENTS", 2)
    monkeypatch.setattr(facade_mod, "bind_ask_human", lambda agent: None)
    made: list[_Agent] = []
    facade = _facade(tmp_path, made)
    await facade._agent_for("a", "/w")
    await facade._agent_for("b", "/w")
    await facade._agent_for("a", "/w")  # «a» снова свежий
    await facade._agent_for("c", "/w")
    assert list(facade._agents) == ["a", "c"]
    assert [agent.closed for agent in made] == [False, True, False]


async def test_busy_agent_is_never_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(facade_mod, "MAX_LIVE_AGENTS", 1)
    monkeypatch.setattr(facade_mod, "bind_ask_human", lambda agent: None)
    made: list[_Agent] = []
    facade = _facade(tmp_path, made)
    await facade._agent_for("running", "/w")
    facade._busy.add("running")
    facade._busy.add("new")
    await facade._agent_for("new", "/w")
    assert set(facade._agents) == {"running", "new"}
    assert not any(agent.closed for agent in made)
