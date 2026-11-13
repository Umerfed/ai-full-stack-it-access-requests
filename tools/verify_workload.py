#!/usr/bin/env python3
"""Reproducible mock workload/restart check; no key or network is required."""
import argparse
import hashlib
import json
import os
import platform
import resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from it_access.exports import export_snapshot, verify_export
from it_access.intake import WorkerOptions, processing_settings, run_pending
from it_access.providers import MockProvider
from it_access.storage import Store


def verify(documents=2000, workers=4):
    if documents < 20 or not 2 <= workers <= 32:
        raise ValueError("Use at least 20 documents and 2–32 workers for this concurrency scenario.")
    started = time.monotonic()
    options = WorkerOptions(workers=workers, backoff_seconds=0)
    mapping = json.loads((ROOT / "tests/mock_responses/fixture-map.json").read_text())
    routes = json.loads((ROOT / "tests/expected/routing.json").read_text())
    cases = []
    for name, fixture in sorted(mapping.items()):
        raw = (ROOT / "materials/requests" / name).read_bytes()
        cases.append((name, raw, processing_settings(mock_reply=(ROOT / "tests/mock_responses" / fixture).read_text())))
    hold, entered, stop = threading.Event(), threading.Event(), threading.Event()
    counter_lock = threading.Lock()
    active = peak = calls = 0
    failures = []
    phase_counts = []

    class ControlledMock:
        def __init__(self, settings):
            self.provider = MockProvider(fixture_text=settings["mock_reply"])

        def complete(self, message, **kwargs):
            nonlocal active, peak, calls
            with counter_lock:
                active += 1
                peak = max(peak, active)
                calls += 1
                first = calls == 1
            try:
                if first:
                    entered.set()
                    if not hold.wait(10):
                        raise TimeoutError
                time.sleep(0.001)  # Declared synthetic provider latency, not live latency.
                return self.provider.complete(message, **kwargs)
            finally:
                with counter_lock:
                    active -= 1

    def ingest_all(store):
        for index in range(documents):
            name, raw, settings = cases[index % len(cases)]
            store.ingest(f"load-{index:05}-{name}", raw, settings)

    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "load.db"
        store = Store(db)
        ingest_all(store)
        prepared = time.monotonic()

        def initial_worker():
            try:
                phase_counts.append(run_pending(store, options=options, stop_event=stop, provider_factory=ControlledMock))
            except Exception as exc:
                failures.append(type(exc).__name__)

        thread = threading.Thread(target=initial_worker)
        thread.start()
        try:
            assert entered.wait(5), "No provider call started"
            deadline = time.monotonic() + 25
            while True:
                current = store.current()
                ready = sum(row["processing_state"] == "completed" for row in current)
                if ready >= min(40, documents // 10):
                    break
                assert thread.is_alive() and time.monotonic() < deadline, "No incremental progress"
                time.sleep(0.02)
            assert any(row["processing_state"] == "running" for row in current)
        finally:
            stop.set()
            hold.set()
            thread.join(15)
        assert not thread.is_alive() and not failures, failures
        before = {row["attempt_id"]: row["claim_count"] for row in store.current() if row["processing_state"] == "completed"}
        assert 0 < len(before) < documents
        interrupted = time.monotonic()
        program = "from it_access.storage import Store; import os,sys; c=Store(sys.argv[1]).claim(lease_seconds=.1); assert c; print(c.attempt_id,flush=True); os._exit(0)"
        child = subprocess.run([sys.executable, "-c", program, str(db)], cwd=ROOT, check=True,
                               capture_output=True, text=True, timeout=5)
        time.sleep(0.15)
        recovered = store.recover_expired()
        assert recovered == 1
        resumed_count = run_pending(store, options=options, provider_factory=ControlledMock)
        processed = time.monotonic()
        current = store.current()
        assert len(current) == documents and len({row["document_id"] for row in current}) == documents
        assert all(row["processing_state"] == "completed" and row["error"] is None for row in current)
        for row in current:
            original = "IT-" + row["document_id"].rsplit("IT-", 1)[1]
            for key in ("status", "owner", "next_action"):
                assert row[key] == routes[original][key], (original, key)
            raw = (ROOT / "materials/requests" / original).read_bytes()
            assert row["source_sha256"] == hashlib.sha256(raw).hexdigest()
            assert row["response_source"] == "mock"
            if row["attempt_id"] in before:
                assert row["claim_count"] == before[row["attempt_id"]], "Completed work was repeated"
        ingest_all(store)
        assert run_pending(store, options=options) == 0
        unchanged = time.monotonic()
        export_snapshot(store, Path(directory) / "outputs")
        assert verify_export(Path(directory) / "outputs")["documents"] == documents
        with sqlite3.connect(db) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == documents
        finished = time.monotonic()
    assert 2 <= peak <= workers
    return {
        "observation": "mock-workload-not-provider-throughput", "response_source": "mock",
        "input_description": "20 supplied source/fixture pairs repeated under independent filenames retaining their IT identifier; no fresh live model extraction",
        "documents": documents, "worker_options": asdict(options), "synthetic_latency_seconds": 0.001,
        "completed_before_stop": len(before), "claimed_before_stop": phase_counts[0],
        "abrupt_claim_attempt_id": int(child.stdout.strip()), "expired_claims_recovered": recovered,
        "claimed_after_resume": resumed_count, "provider_calls": calls, "peak_provider_concurrency": peak,
        "assertions": {"incremental_results_with_blocked_call": True, "bounded_concurrency": True,
                       "completed_work_not_repeated": True, "expired_claim_recovered": True,
                       "all_routes_match_source_expectations": True, "unchanged_rerun_zero_calls": True,
                       "snapshot_pair_verified": True, "sqlite_integrity_and_foreign_keys": True},
        "durations_seconds": {"prepare": prepared-started, "first_phase": interrupted-prepared,
                              "recovery_and_resume": processed-interrupted, "unchanged_and_inspection": unchanged-processed,
                              "export_and_verify": finished-unchanged, "total": finished-started},
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.platform(), "cpu_count": os.cpu_count(),
                        "max_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss},
        "limits": "One machine/local disk, one bounded-worker scheduler plus an abruptly exited claiming subprocess. Two-operator behavior is separately unit-tested. No actual provider latency/rate/availability or multi-host claim is established.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path("deliverables/workload.json"))
    args = parser.parse_args()
    with mock.patch.object(socket, "socket", side_effect=AssertionError("Unexpected network access")):
        result = verify(args.documents, args.workers)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
