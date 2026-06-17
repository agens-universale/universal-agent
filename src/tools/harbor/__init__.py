from typing import Any, Dict


def _entry(tool_cls) -> Dict[str, Any]:
    return {
        "tool_fabric": tool_cls,
        "spec": tool_cls.tool_description(),
        "description": tool_cls.prompt_description(),
    }


def harbor_registry() -> Dict[str, Dict[str, Any]]:
    from tools.harbor.read_file import HarborReadFile
    from tools.harbor.write_to_file_tool import HarborWriteToFile
    from tools.harbor.append_to_file import HarborAppendToFile
    from tools.harbor.apply_file_edits import HarborApplyFileEdits
    from tools.harbor.search_and_replace import HarborSearchAndReplace
    from tools.harbor.list_files import HarborListFiles
    from tools.harbor.search_files import HarborSearchFiles
    from tools.harbor.file_info import HarborFileInfo
    from tools.harbor.save_base64_file import HarborSaveBase64File
    from tools.harbor.execute_command import HarborExecuteCommand
    from tools.harbor.run_python_code import HarborRunPythonCode

    return {
        "fs_read_file": _entry(HarborReadFile),
        "fs_write_file": _entry(HarborWriteToFile),
        "fs_append_file": _entry(HarborAppendToFile),
        "fs_apply_file_edits": _entry(HarborApplyFileEdits),
        "fs_search_and_replace": _entry(HarborSearchAndReplace),
        "fs_list_files": _entry(HarborListFiles),
        "fs_search_files": _entry(HarborSearchFiles),
        "fs_file_info": _entry(HarborFileInfo),
        "fs_save_base64_file": _entry(HarborSaveBase64File),
        "exec_command": _entry(HarborExecuteCommand),
        "exec_python": _entry(HarborRunPythonCode),
    }
