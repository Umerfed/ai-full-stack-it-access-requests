"""Operator CLI entry point; intake commands are added in later build steps."""
import argparse
import json
import hashlib
import os
import signal
import sqlite3
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from it_access.intake import WorkerOptions, processing_settings, run_pending
from it_access.extraction import ExtractionError, instructions_from_settings, message_from_export
from it_access.openai_provider import DEFAULT_MODEL, request_evidence
from it_access.providers import MockProvider, ProviderError
from it_access.storage import Store


def _worker_flags(parser):
    defaults = WorkerOptions()
    parser.add_argument("--workers", type=int, default=defaults.workers)
    parser.add_argument("--timeout-seconds", type=float, default=defaults.timeout_seconds)
    parser.add_argument("--max-attempts", type=int, default=defaults.max_attempts)
    parser.add_argument("--lease-seconds", type=float, default=defaults.lease_seconds)
    parser.add_argument("--backoff-seconds", type=float, default=defaults.backoff_seconds)
    parser.add_argument("--max-backoff-seconds", type=float, default=defaults.max_backoff_seconds)
    parser.add_argument("--requests-per-minute", type=float, default=defaults.requests_per_minute,
                        help="Shared DB/provider/model request limit; 0 disables spacing.")


def _model_flags(parser):
    parser.add_argument("--model", help="OpenAI model ID; defaults to MODEL_ID or the documented pinned snapshot.")
    parser.add_argument("--max-output-tokens", type=int, help="Defaults to MAX_OUTPUT_TOKENS or 4096.")
    parser.add_argument("--reasoning-effort", help="Defaults to REASONING_EFFORT or none.")


def _openai_settings(args):
    try:
        tokens = args.max_output_tokens if args.max_output_tokens is not None else int(os.environ.get("MAX_OUTPUT_TOKENS", "4096"))
    except ValueError:
        raise ValueError("MAX_OUTPUT_TOKENS must be a valid integer.") from None
    return processing_settings(provider="openai", model=args.model if args.model is not None else os.environ.get("MODEL_ID", DEFAULT_MODEL),
        generation={"max_output_tokens": tokens, "reasoning_effort": args.reasoning_effort if args.reasoning_effort is not None else os.environ.get("REASONING_EFFORT", "none")})


def _run_jobs(args, store, *, document_id=None):
    stop = threading.Event()
    received = [None]

    def interrupt(signum, frame):
        received[0] = signum
        stop.set()

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupt)
    try:
        processed = run_pending(store, options=args.worker_options, stop_event=stop, document_id=document_id,
                                provider_name=args.provider)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    remaining = sum(row["processing_state"] in {"pending", "running"} for row in store.current())
    return {"processed": processed, "unfinished": remaining, "interrupted": stop.is_set(), "signal": received[0]}


def _intake(args) -> dict:
    if args.provider == "mock":
        if args.fixture_map is None:
            raise ValueError("Mock intake requires --fixture-map.")
        if any(value is not None for value in (args.model, args.max_output_tokens, args.reasoning_effort)):
            raise ValueError("Model generation flags require --provider openai.")
        mapping_path = args.fixture_map.resolve()
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            raise ValueError("Fixture map must be a JSON object of input filenames to fixture paths.")
    else:
        if args.fixture_map is not None:
            raise ValueError("OpenAI intake does not use --fixture-map.")
        live_settings = _openai_settings(args)
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
        if args.provider == "mock":
            fixture = mapping.get(path.name)
            if not isinstance(fixture, str):
                raise ValueError("Every input filename needs a fixture path in --fixture-map.")
            fixture_path = mapping_path.parent / fixture
            settings = processing_settings(mock_reply=fixture_path.read_text(encoding="utf-8"))
        else:
            settings = live_settings
        prepared.append((path.name, path.read_bytes(), settings))
    store = Store(args.db)
    attempts = [store.ingest(name, raw, settings) for name, raw, settings in prepared]
    work = {"processed": 0} if args.prepare_only else _run_jobs(args, store)
    return {"accepted": len(paths), "attempt_ids": attempts, **work}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="it_access", description="Prepare IT access requests for human review."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "mock-reply", help="Replay a local mock reply (no extraction or intake)."
    )
    replay.add_argument("--fixture", required=True, help="Path to a saved mock JSON reply.")
    intake = commands.add_parser("intake", help="Persist inputs and process pending extractions for the chosen provider.")
    intake.add_argument("inputs", nargs="+", type=Path, help="JSON exports or directories of exports.")
    intake.add_argument("--provider", choices=["mock", "openai"], default="mock")
    intake.add_argument("--fixture-map", type=Path,
                        help="JSON map: input filename to mock fixture path, relative to this map.")
    intake.add_argument("--prepare-only", action="store_true", help="Persist pending work without processing.")
    intake.add_argument("--db", type=Path, default=Path("var/intake.db"))
    _worker_flags(intake)
    _model_flags(intake)
    resume = commands.add_parser("resume", help="Recover expired claims and process unfinished work.")
    resume.add_argument("--db", type=Path, default=Path("var/intake.db"))
    resume.add_argument("--provider", choices=["mock", "openai"], help="Only process pending jobs for this saved provider.")
    _worker_flags(resume)
    retry = commands.add_parser("retry", help="Create a fresh attempt for a completed or failed document.")
    retry.add_argument("document_id")
    retry.add_argument("--db", type=Path, default=Path("var/intake.db"))
    retry.add_argument("--provider", choices=["mock", "openai"], help="Require this provider to match the saved attempt.")
    retry.add_argument("--fixture", type=Path, help="Replace the saved mock reply for this new attempt.")
    retry.add_argument("--prepare-only", action="store_true")
    _worker_flags(retry)
    evidence = commands.add_parser("request-evidence", help="Write a credential-redacted model request example without sending it.")
    evidence.add_argument("input", type=Path)
    evidence.add_argument("--out", type=Path, default=Path("deliverables/request-evidence.json"))
    _model_flags(evidence)
    _worker_flags(evidence)
    records = commands.add_parser("records", help="Inspect latest extraction attempt and routing per document.")
    records.add_argument("--db", type=Path, default=Path("var/intake.db"))
    queue = commands.add_parser("queue", help="Inspect the current human review queue and unfinished work.")
    queue.add_argument("--db", type=Path, default=Path("var/intake.db"))
    history = commands.add_parser("history", help="Inspect all attempts for an input filename.")
    history.add_argument("document_id")
    history.add_argument("--db", type=Path, default=Path("var/intake.db"))
    args = parser.parse_args(argv)
    try:
        if args.command in {"intake", "resume", "retry", "request-evidence"}:
            args.worker_options = WorkerOptions(**{field: getattr(args, field) for field in WorkerOptions.__dataclass_fields__})
        if args.command == "mock-reply":
            result = asdict(MockProvider(args.fixture).complete(""))
        elif args.command == "intake":
            result = _intake(args)
        elif args.command == "resume":
            store = Store(args.db)
            recovered = store.recover_expired()
            result = {"recovered": recovered, **_run_jobs(args, store)}
        elif args.command == "retry":
            overrides = {"mock_reply": args.fixture.read_text(encoding="utf-8")} if args.fixture else None
            store = Store(args.db)
            target = next((row for row in store.current() if row["document_id"] == args.document_id), None)
            if target is not None:
                saved_provider = target["settings"]["provider"]
                if args.provider is not None and args.provider != saved_provider:
                    raise ValueError("Retry provider must match the saved attempt; use intake to change provider.")
                if args.fixture is not None and saved_provider != "mock":
                    raise ValueError("--fixture is supported only for mock retries.")
            attempt_id = store.retry(args.document_id, settings_overrides=overrides)
            work = {"processed": 0} if args.prepare_only else _run_jobs(args, store, document_id=args.document_id)
            result = {"attempt_id": attempt_id, **work}
        elif args.command == "request-evidence":
            raw = args.input.read_bytes()
            exported = json.loads(raw.decode("utf-8"))
            message = message_from_export(exported)
            settings = _openai_settings(args)
            example = request_evidence(settings, message, instructions_from_settings(settings),
                                       worker_options=asdict(args.worker_options))
            example["source"] = {"document_id": args.input.name, "source_sha256": hashlib.sha256(raw).hexdigest()}
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(example, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            result = {"observation": example["observation"], "path": str(args.out)}
        elif args.command == "records":
            result = Store(args.db).current()
        elif args.command == "queue":
            result = Store(args.db).queue()
        else:
            result = Store(args.db).history(args.document_id)
    except (ExtractionError, ProviderError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("error: Database operation failed; check its location, permissions and concurrent writers.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 128 + result["signal"] if isinstance(result, dict) and result.get("signal") else 0
