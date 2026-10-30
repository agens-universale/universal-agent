"""``gc-plugin``: установка и управление плагинами."""

from __future__ import annotations

import argparse
import sys

from gc_core.plugins import installed, sources


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
    src = sub.add_parser("sources", help="git-источники plugins.sources")
    src_sub = src.add_subparsers(dest="sources_cmd", required=True)
    src_sub.add_parser(
        "update",
        help="обновить кеш источников на ветке (теги и коммиты — нет)",
    )
    return parser


def _row(p: installed.InstalledPlugin) -> str:
    state = "on " if p.enabled else "off"
    pin = (p.commit or "")[:8]
    ref = f"@{p.ref}" if p.ref else ""
    return f"{state} {p.name} {p.version}  {p.source}{ref} {pin}".rstrip()


def _role_settings(prepared: installed.PreparedInstall) -> object | None:
    """Настройки для прав ролей в сводке; без ролей не загружаются."""
    if not prepared.plugin.agents:
        return None
    from gc_core.config import load_settings
    from gc_core.settings import default_settings

    try:
        return load_settings()
    except Exception:  # битый конфиг не мешает установке: берём умолчания
        return default_settings()


def _update_sources() -> int:
    done = sources.update_cached()
    for name, ok in done:
        print(f"{'обновлён' if ok else 'не обновлён'}: {name}")
    if not done:
        print("нечего обновлять")
    return 0 if all(ok for _, ok in done) else 1


def _confirm(prepared: installed.PreparedInstall, yes: bool) -> bool:
    """Показать, что плагин запускает, и спросить; без TTY нужен ``--yes``."""
    plugin = prepared.plugin
    print(f"плагин {plugin.name} {plugin.version} запускает:")
    for line in prepared.summary(_role_settings(prepared)):
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
        elif args.cmd == "sources":
            return _update_sources()
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
