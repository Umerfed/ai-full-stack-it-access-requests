"""Operator CLI entry point; intake commands are added in later build steps."""
import argparse
import json
import sys
from dataclasses import asdict

from it_access.providers import MockProvider, ProviderError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="it_access", description="Prepare IT access requests for human review."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "mock-reply", help="Replay a local mock reply (no extraction or intake)."
    )
    replay.add_argument("--fixture", required=True, help="Path to a saved mock JSON reply.")
    args = parser.parse_args(argv)
    try:
        response = MockProvider(args.fixture).complete("")
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(response), ensure_ascii=False))
    return 0
