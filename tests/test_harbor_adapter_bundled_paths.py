# tests/test_harbor_adapter_bundled_paths.py
import importlib.util, os, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

def _load_adapter():
    # tests/harbor/ (a test-fixture package with __init__.py) is added to sys.path by
    # pytest before site-packages, shadowing the real harbor framework.  Ensure the
    # harbor venv site-packages directory appears first so the adapter's framework
    # imports ("from harbor.agents.base import BaseAgent" etc.) resolve correctly.
    _harbor_sp = next(
        (p for p in sys.path
         if (Path(p) / "harbor" / "agents" / "base.py").exists()),
        None,
    )
    if _harbor_sp:
        sys.path.remove(_harbor_sp)
        sys.path.insert(0, _harbor_sp)
    # Evict any cached shadow from tests/harbor/ so the re-inserted site-packages wins.
    for key in list(sys.modules.keys()):
        if key == "harbor" or key.startswith("harbor."):
            del sys.modules[key]
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "gc_cli_external", REPO / "harbor" / "adapters" / "gc_cli_external.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.GcCliExternal

def test_adapter_points_dependency_paths_to_existing_files():
    GcCliExternal = _load_adapter()
    a = GcCliExternal(logs_dir=Path("/tmp"), model_name="openai/m")
    c = a._build_config()
    # system prompt is the BASE prompt, and exists
    assert c["chat_completions_system_prompt_path"].endswith("prompts/chat_completions_system_prompt.md")
    assert os.path.isfile(c["chat_completions_system_prompt_path"])
    # skills dir set + exists
    assert os.path.isdir(c["skills_data_path"])
    # summary sub-prompt paths exist
    sp = c["summary_params"]
    for k in ("chat_completions_summary_prompt_path",
              "chat_completions_memory_patch_append_prompt_path",
              "chat_completions_memory_patch_replace_prompt_path"):
        assert os.path.isfile(sp[k]), k
    # intent_guard prompt path is bundled + exists (activates the finish gate in the
    # image; without it the gate fail-opens because its default path is source-only)
    assert c["chat_completions_intent_guard_prompt_path"].endswith("prompts/chat_completions_intent_guard_prompt.md")
    assert os.path.isfile(c["chat_completions_intent_guard_prompt_path"])

def test_profile_overrides_still_win():
    GcCliExternal = _load_adapter()
    a = GcCliExternal(logs_dir=Path("/tmp"), model_name="openai/m",
                      gc_cli_config={"use_local_subagents": True,
                                     "summary_params": {"chat_completions_summary_trigger_chars": 150000}})
    c = a._build_config()
    assert c["use_local_subagents"] is True
    # merged: profile trigger + adapter-provided summary prompt path coexist
    assert c["summary_params"]["chat_completions_summary_trigger_chars"] == 150000
    assert os.path.isfile(c["summary_params"]["chat_completions_summary_prompt_path"])
