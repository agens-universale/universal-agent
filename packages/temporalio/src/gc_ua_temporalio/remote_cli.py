"""Remote CLI entry used by gc-cli --temporal-url and gc-temporal-remote."""

import argparse
import asyncio
import json
import uuid

from gc_ua_temporalio.remote import RemoteCursor, RemoteSessionProvider


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporal-url", required=True)
    parser.add_argument("--system", default="temporal")
    parser.add_argument("--thread", required=True)
    parser.add_argument("--message")
    parser.add_argument("--message-id")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--close-session", action="store_true")
    parser.add_argument("--answer")
    parser.add_argument("--request-id")
    return parser.parse_args(argv)


async def watch(provider):
    cursor = RemoteCursor()
    while True:
        page = await provider.history(cursor.history)
        for row in cursor.apply_history(page):
            print(json.dumps(row["message"], ensure_ascii=False), flush=True)
        status = await provider.status()
        active = status.get("child") or status
        if active.get("phase", status["phase"]) in (
            "waiting_human",
            "needs_reconciliation",
            "session_lost",
        ) or status["phase"] in (
            "idle",
            "closed",
            "failed",
            "stopped",
            "waiting_human",
            "needs_reconciliation",
            "session_lost",
        ):
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return
        await asyncio.sleep(0.5)


async def run(args):
    provider = RemoteSessionProvider(
        args.temporal_url, args.system, args.thread
    )
    try:
        if args.message is not None:
            # Print the ID before submitting so a lost HTTP response can be retried.
            message_id = args.message_id or uuid.uuid4().hex
            print(json.dumps({"message_id": message_id}), flush=True)
            print(await provider.send(message_id, args.message))
        if args.stop:
            print(await provider.stop((await provider.status())["turn_id"]))
        if args.answer is not None:
            if not args.request_id:
                raise ValueError("--answer requires --request-id")
            print(
                await provider.answer(
                    args.request_id, uuid.uuid4().hex, args.answer
                )
            )
        if args.close_session:
            print(await provider.close_session())
        if args.watch:
            await watch(provider)
        else:
            print(
                json.dumps(
                    await provider.status(), ensure_ascii=False, indent=2
                )
            )
    finally:
        await provider.aclose()


def main(argv=None):
    asyncio.run(run(arguments(argv)))
    return 0
