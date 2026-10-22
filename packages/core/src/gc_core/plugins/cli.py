"""``gc-plugin``: установка и управление плагинами."""

from __future__ import annotations

import argparse
import sys

from gc_core.plugins import installed


def _build() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gc-plugin",
        description="Установленные плагины: ставятся в хранилище и работают "
        "без правки конфига.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="установить плагин")
    p.add_argument("source", help="путь, URL или owner/repo (GitHub)")
    p.add_argument("--ref", help="ветка, тег или хеш коммита")
    p.add_argument("--subdir", help="каталог плагина внутри репозитория")
    p.add_argument(
        "--force", action="store_true", help="заменить установленный"
    )
    p.add_argument(
        "--yes", "-y", action="store_true", help="не спрашивать подтверждение"
    )
    for name, text in (
        ("uninstall", "удалить"),
        ("enable", "включить"),
        ("disable", "выключить (файлы остаются)"),
        ("update", "обновить из источника"),
    ):
        q = sub.add_parser(name, help=text)
        q.add_argument("name")
        if name == "update":
            q.add_argument(
                "--yes",
                "-y",
                action="store_true",
                help="не спрашивать подтверждение",
            )
    sub.add_parser("list", help="список установленных")
    return parser


def _row(p: installed.InstalledPlugin) -> str:
    state = "on " if p.enabled else "off"
    pin = (p.commit or "")[:8]
    ref = f"@{p.ref}" if p.ref else ""
    return f"{state} {p.name} {p.version}  {p.source}{ref} {pin}".rstrip()


def _confirm(prepared: installed.PreparedInstall, yes: bool) -> bool:
    """Показать, что плагин запускает, и спросить; без TTY нужен ``--yes``."""
    plugin = prepared.plugin
    print(f"плагин {plugin.name} {plugin.version} запускает:")
    for line in prepared.summary():
        print(f"  {line}")
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("нет TTY: подтвердите установку флагом --yes")
    try:
        answer = input("Установить? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in (
        "y",
        "yes",
        "д",
        "да",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build().parse_args(argv)
    try:
        if args.cmd == "install":
            with installed.prepare(
                args.source, ref=args.ref, subdir=args.subdir
            ) as prepared:
                prepared.ensure_free(args.force)
                if not _confirm(prepared, args.yes):
                    print("установка отменена", file=sys.stderr)
                    return 1
                info = prepared.commit(args.force)
            print(f"установлен: {_row(info)}")
        elif args.cmd == "list":
            rows = installed.list_installed()
            print("\n".join(_row(p) for p in rows) or "плагинов нет")
        elif args.cmd == "update":
            info = installed.update(
                installed.resolve_name(args.name),
                confirm=lambda prepared: _confirm(prepared, args.yes),
            )
            if info is None:
                print(f"не установлен: {args.name}", file=sys.stderr)
                return 1
            print(f"обновлён: {_row(info)}")
        else:
            actions = {
                "uninstall": installed.uninstall,
                "enable": installed.enable,
                "disable": installed.disable,
            }
            ok = actions[args.cmd](installed.resolve_name(args.name))
            if not ok:
                print(f"не установлен: {args.name}", file=sys.stderr)
                return 1
            print(f"{args.cmd}: {args.name}")
    except (ValueError, OSError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # git и сеть
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
