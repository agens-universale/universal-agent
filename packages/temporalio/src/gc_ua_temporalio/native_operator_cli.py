"""Inspect/settle/release a terminal native run without resetting execution."""

import argparse
import asyncio
import json
from pathlib import Path

from temporalio.client import Client
from temporalio.converter import value_to_type

from gc_ua_temporalio.native_closed_recovery import NativeClosedRecovery
from gc_ua_temporalio.native_operator_contracts import NativeOperatorCall
from gc_ua_temporalio.native_provider import NativeWorkerConfig


async def run(args):
    config = NativeWorkerConfig.load(args.config)
    client = await Client.connect(config.address, namespace=config.namespace)
    recovery = NativeClosedRecovery(config, client, args.store)
    if args.command == "inspect-closed":
        result = await recovery.inspect(args.system, args.thread)
    elif args.command == "resolve-closed":
        value = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        result = await recovery.resolve(
            args.system, args.thread, value_to_type(NativeOperatorCall, value)
        )
    else:
        result = await recovery.release(
            args.system, args.thread, args.request_id, evidence=args.evidence
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--thread", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect-closed")
    resolve = commands.add_parser("resolve-closed")
    resolve.add_argument("--receipt", required=True)
    release = commands.add_parser("release-thread")
    release.add_argument("--request-id", required=True)
    release.add_argument("--evidence", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
