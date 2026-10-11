"""``thread-file://`` в охране чтения: ссылку на файл треда открывает только
``fs_view_image``.

Прочие тулы чтения понимают строку как относительный путь ФС
(``<workspace>/thread-file:/<sys>/<thread>/…``). Пропуск охраны для них —
обход периметра: симлинк ``thread-file:/cli/T1`` в рабочей папке, ведущий
наружу, отдавал бы файл, прямой путь к которому охрана закрывает.
"""

import json
import os

from gc_core.tools.fs.read_file import ReadFile
from gc_testkit.helpers.tool_ctx import local_ctx

SECRET = "sk-MUST-NOT-LEAK"


async def test_read_tools_refuse_thread_file_links(tmp_path, monkeypatch):
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "id_rsa").write_text(SECRET)
    (workspace / "thread-file:" / "cli").mkdir(parents=True)
    os.symlink(outside, workspace / "thread-file:" / "cli" / "T1")
    monkeypatch.chdir(workspace)
    ctx = local_ctx(
        {
            "launch_root": str(workspace),
            "security.read_guard": {"enabled": True, "mode": "deny"},
        }
    )
    ctx.system_id, ctx.thread_id = "cli", "T1"
    tool = ReadFile(ctx=ctx)

    direct = await tool.acall(path=str(outside / "id_rsa"))
    assert SECRET not in direct
    raw = await tool.acall(path="thread-file://cli/T1/id_rsa")
    assert SECRET not in raw
    assert json.loads(raw)["error"]["type"] == "read_denied"
