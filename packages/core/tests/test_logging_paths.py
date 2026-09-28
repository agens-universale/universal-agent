from pathlib import Path

import pytest
from gc_core.utils.logging_module import build_run_log_paths, create_run_logger


def test_build_run_log_paths_uses_logs_dump_dir(tmp_path):
    log_path, _ = build_run_log_paths(
        config_dict={"logs_dump_dir": str(tmp_path)},
        run_id="abc",
        agent_kind="main",
    )
    assert Path(log_path).parent == tmp_path
    assert Path(log_path).name == "agent_main_abc.log"


def test_build_run_log_paths_raises_without_logs_dump_dir():
    with pytest.raises(ValueError, match="logs_dump_dir"):
        build_run_log_paths(config_dict={}, run_id="abc", agent_kind="main")


def test_create_run_logger_creates_parent_directory(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    logger, log_path, _ = create_run_logger(
        config_dict={"logs_dump_dir": str(deep), "save_logs": True},
        run_id="abc",
        agent_kind="main",
    )
    assert deep.exists() and deep.is_dir()
    assert Path(log_path).parent == deep
    for h in logger.handlers:
        h.close()
    logger.handlers.clear()
