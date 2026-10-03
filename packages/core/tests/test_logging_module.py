from gc_core.utils.logging_module import (
    build_run_log_paths,
    create_run_logger,
    generate_run_id,
)
from gc_testkit.settings import make_settings


def test_build_run_log_paths_include_agent_kind_and_run_id(tmp_path):
    cfg = make_settings({"logs_dump_dir": str(tmp_path)})
    run_id = "run123"

    logging_path, summary_path = build_run_log_paths(
        cfg, run_id=run_id, agent_kind="subagent_worker"
    )

    assert logging_path.endswith("agent_worker_run123.log")
    assert summary_path.endswith("summary_trace_worker_run123.jsonl")


def test_create_run_logger_uses_unique_logger_name_and_files(tmp_path):
    cfg = make_settings({"save_logs": True, "logs_dump_dir": str(tmp_path)})
    run_id_1 = generate_run_id()
    run_id_2 = generate_run_id()

    logger_1, path_1, _ = create_run_logger(
        cfg, run_id=run_id_1, agent_kind="main"
    )
    logger_2, path_2, _ = create_run_logger(
        cfg, run_id=run_id_2, agent_kind="main"
    )

    assert logger_1.name != logger_2.name
    assert path_1 != path_2
    assert path_1.endswith(f"agent_main_{run_id_1}.log")
    assert path_2.endswith(f"agent_main_{run_id_2}.log")
