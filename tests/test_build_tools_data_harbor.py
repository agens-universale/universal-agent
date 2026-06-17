from tools import build_tools_data


def test_harbor_pack_off_by_default():
    cfg = {"backend_api_mode": "chat_completions"}
    tools_data, _, _, _ = build_tools_data(cfg)
    assert tools_data["fs_read_file"]["tool_fabric"].__name__ == "ReadFile"
    assert "download_file" in tools_data


def test_harbor_pack_overrides_when_enabled():
    cfg = {"backend_api_mode": "chat_completions", "use_harbor_tools": True}
    tools_data, _, _, _ = build_tools_data(cfg)
    assert tools_data["fs_read_file"]["tool_fabric"].__name__ == "HarborReadFile"
    assert tools_data["exec_command"]["tool_fabric"].__name__ == "HarborExecuteCommand"
    assert "download_file" not in tools_data


def test_all_eleven_harbor_tools_registered():
    from tools.harbor import harbor_registry
    keys = set(harbor_registry())
    expected = {
        "fs_read_file", "fs_write_file", "fs_append_file", "fs_apply_file_edits",
        "fs_search_and_replace", "fs_list_files", "fs_search_files", "fs_file_info",
        "fs_save_base64_file", "exec_command", "exec_python",
    }
    assert keys == expected
    # every harbor tool keeps its builtin registry key as its tool name
    for key, entry in harbor_registry().items():
        assert entry["tool_fabric"].registered_tool_name() == key
