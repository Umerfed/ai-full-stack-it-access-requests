"""Operator CLI entry point; intake commands are added in later build steps."""
import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from it_access.intake import processing_settings, run_pending
from it_access.providers import MockProvider, ProviderError
from it_access.storage import Store


def _intake(args) -> dict:
    mapping_path = args.fixture_map.resolve()
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise ValueError("Fixture map must be a JSON object of input filenames to fixture paths.")
    paths = []
    for item in args.inputs:
        if item.is_dir():
            paths.extend(sorted(item.glob("*.json")))
        else:
            paths.append(item)
    if not paths:
        raise ValueError("No JSON source files found.")
    if len({path.name for path in paths}) != len(paths):
        raise ValueError("Input filenames must be unique within a batch.")
    prepared = []
    for path in paths:
        fixture = mapping.get(path.name)
        if not isinstance(fixture, str):
            raise ValueError("Every input filename needs a fixture path in --fixture-map.")
        fixture_path = mapping_path.parent / fixture
        settings = processing_settings(mock_reply=fixture_path.read_text(encoding="utf-8"))
        prepared.append((path.name, path.read_bytes(), settings))
    store = Store(args.db)
    attempts = [store.ingest(name, raw, settings) for name, raw, settings in prepared]
    processed = 0 if args.prepare_only else run_pending(store)
    return {"accepted": len(paths), "attempt_ids": attempts, "processed": processed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="it_access", description="Prepare IT access requests for human review."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "mock-reply", help="Replay a local mock reply (no extraction or intake)."
    )
    replay.add_argument("--fixture", required=True, help="Path to a saved mock JSON reply.")
    intake = commands.add_parser("intake", help="Persist inputs and process pending mock extractions.")
    intake.add_argument("inputs", nargs="+", type=Path, help="JSON exports or directories of exports.")
    intake.add_argument("--provider", choices=["mock"], default="mock")
    intake.add_argument("--fixture-map", type=Path, required=True,
                        help="JSON map: input filename to mock fixture path, relative to this map.")
    intake.add_argument("--prepare-only", action="store_true", help="Persist pending work without processing.")
    intake.add_argument("--db", type=Path, default=Path("var/intake.db"))
    records = commands.add_parser("records", help="Inspect latest extraction attempt and routing per document.")
    records.add_argument("--db", type=Path, default=Path("var/intake.db"))
    queue = commands.add_parser("queue", help="Inspect the current human review queue and unfinished work.")
    queue.add_argument("--db", type=Path, default=Path("var/intake.db"))
    history = commands.add_parser("history", help="Inspect all attempts for an input filename.")
    history.add_argument("document_id")
    history.add_argument("--db", type=Path, default=Path("var/intake.db"))
    args = parser.parse_args(argv)
    try:
        if args.command == "mock-reply":
            result = asdict(MockProvider(args.fixture).complete(""))
        elif args.command == "intake":
            result = _intake(args)
        elif args.command == "records":
            result = Store(args.db).current()
        elif args.command == "queue":
            result = Store(args.db).queue()
        else:
            result = Store(args.db).history(args.document_id)
    except (ProviderError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("error: Database operation failed; check its location, permissions and concurrent writers.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
