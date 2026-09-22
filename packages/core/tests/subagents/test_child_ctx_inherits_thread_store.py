"""Дочерний контекст субагента наследует хранилище тредов родителя.

`apersist_subagent_trace` пишет трассу в `ctx.thread_store`, а без него — в
`FileThreadStore(threads_dir)`. Дочерний контекст собирается руками, и поле
туда не копировалось, хотя `ask_human`, `tui_confirm` и `permission_store`
копируются. Для встраивающего, отдавшего свой стор
(`AsyncAgent.create(cfg, thread_store=…)`), это значит одно из двух: трасса
субагента терялась или ложилась на диск хоста мимо его хранилища.

Проверка структурная. Поведенческая потребовала бы поднять весь
`_build_child_ctx` — с дискавери тулов, реестром скиллов и роутером, — то есть
подменить половину сборки; такая проверка ловила бы собственные подмены, а не
копирование поля.
"""
import ast
import pathlib

from gc_core.tools.subagents import runner as runner_module

#: Поля, которые дочерний контекст обязан взять у родителя. Список не
#: декоративный: каждое сюда попало после отдельной поломки — порт ответа
#: человека, баннер подтверждения, хранилище решений, теперь хранилище тредов.
INHERITED = ("ask_human", "tui_confirm", "permission_store", "thread_store")


def _build_child_ctx_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(pathlib.Path(runner_module.__file__).read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_build_child_ctx"
    )


def _assigned_from_parent(node: ast.AsyncFunctionDef) -> set:
    """Поля `child_ctx.X = getattr(self._ctx, "X", …)`."""
    out = set()
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not (isinstance(target, ast.Attribute)
                    and getattr(target.value, "id", None) == "child_ctx"):
                continue
            call = stmt.value
            if (isinstance(call, ast.Call)
                    and getattr(call.func, "id", None) == "getattr"
                    and call.args
                    and isinstance(call.args[0], ast.Attribute)
                    and call.args[0].attr == "_ctx"):
                out.add(target.attr)
    return out


def test_child_inherits_the_thread_store():
    inherited = _assigned_from_parent(_build_child_ctx_node())

    assert "thread_store" in inherited, (
        "дочерний контекст не наследует thread_store: трасса субагента уйдёт "
        "в файловое хранилище мимо стора встраивающего"
    )


def test_the_whole_inherited_set_is_still_there():
    """Соседние поля из того же списка — чтобы правка не сняла одно из них."""
    inherited = _assigned_from_parent(_build_child_ctx_node())
    missing = [name for name in INHERITED if name not in inherited]

    assert not missing, f"дочерний контекст перестал наследовать: {missing}"
