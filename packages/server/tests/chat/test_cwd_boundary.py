"""Каталог хода chat — только внутри ``chat.launch_root``, как у A2A.

Каталог хода — корень записи агента, а с хранилищем агента в базе ещё и
ключ проекта: клиент chat с любым путём получал бы память и скиллы чужого
проекта. Проверяется раскрытый путь (симлинки, «..»)."""

import os

import pytest
from gc_server.chat.completions.facade import CompletionsFacade
from gc_server.chat.cwd import cwd_for_new
from gc_server.chat.errors import ApiError
from gc_server.chat.responses.facade import ResponsesFacade
from gc_server.chat.responses.index_store import ResponseIndex
from gc_server.chat.responses.input_text import cwd_for_continue
from gc_server.config import ChatConfig
from gc_testkit.helpers.crossplatform import require_symlinks
from gc_testkit.settings import make_settings


@pytest.fixture()
def root(tmp_path):
    path = tmp_path / "root"
    path.mkdir()
    return path


def _refused(sent: str, launch_root) -> ApiError:
    with pytest.raises(ApiError) as caught:
        cwd_for_new(sent, launch_root)
    assert caught.value.status == 400
    return caught.value


def test_cwd_inside_the_root_is_taken_resolved(root):
    work = root / "proj"
    work.mkdir()
    sent = str(root / "proj" / ".." / "proj")
    assert cwd_for_new(sent, str(root)) == os.path.realpath(work)


def test_cwd_outside_the_root_is_refused(tmp_path, root):
    other = tmp_path / "other"
    other.mkdir()
    assert "launch_root" in _refused(str(other), str(root)).message


def test_sibling_with_the_same_prefix_is_outside(tmp_path, root):
    evil = tmp_path / "root-evil"
    evil.mkdir()
    _refused(str(evil), str(root))


def test_dot_dot_out_of_the_root_is_refused(root):
    _refused(str(root / ".." / "elsewhere"), str(root))


def test_symlink_out_of_the_root_is_refused(tmp_path, root):
    require_symlinks(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "link", target_is_directory=True)
    _refused(str(root / "link"), str(root))


def test_without_launch_root_the_process_dir_is_the_boundary(
    tmp_path, root, monkeypatch
):
    monkeypatch.chdir(root)
    (root / "proj").mkdir()
    assert cwd_for_new(str(root / "proj"), None) == os.path.realpath(
        root / "proj"
    )
    _refused(str(tmp_path), None)


def test_no_cwd_sent_is_the_root(root):
    assert cwd_for_new(None, str(root)) == os.path.normpath(root)


def test_continue_accepts_the_same_dir_by_another_spelling(tmp_path, root):
    """Записан раскрытый путь — продолжение тем же путём через симлинк не
    «чужой каталог»."""
    require_symlinks(tmp_path)
    os.symlink(root, tmp_path / "alias", target_is_directory=True)
    recorded = os.path.realpath(root)
    assert cwd_for_continue(str(tmp_path / "alias"), recorded) == recorded


class _Factory:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, settings, **kwargs):
        self.calls += 1
        raise AssertionError("агент не должен создаваться")


async def test_responses_refuse_a_foreign_cwd_before_the_agent(tmp_path, root):
    threads = tmp_path / "threads"
    threads.mkdir()
    factory = _Factory()
    facade = ResponsesFacade(
        settings=make_settings(**{"agent.model": "m"}),
        section=ChatConfig(host="127.0.0.1", launch_root=str(root)),
        agent_factory=factory,
        index=ResponseIndex.for_threads_dir(str(threads)),
    )
    with pytest.raises(ApiError) as caught:
        await facade.open(
            {"model": "m", "input": "hi", "metadata": {"cwd": str(tmp_path)}}
        )
    assert caught.value.status == 400
    assert factory.calls == 0
    assert not facade.turn_busy("any")


async def test_completions_refuse_a_foreign_cwd_before_the_agent(
    tmp_path, root
):
    factory = _Factory()
    facade = CompletionsFacade(
        settings=make_settings(
            **{"agent.model": "m"},
            **{"paths.threads_data_path": str(tmp_path / "threads")},
        ),
        section=ChatConfig(host="127.0.0.1", launch_root=str(root)),
        agent_factory=factory,
    )
    with pytest.raises(ApiError) as caught:
        await facade.open(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"cwd": str(tmp_path)},
            }
        )
    assert caught.value.status == 400
    assert factory.calls == 0
