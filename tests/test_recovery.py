"""T21/T22/T24/T28/T29: scheduling, interruption and bounded recovery, offline."""
import io
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from test_mock_provider import OfflineTestCase
from it_access.cli import main
from it_access.intake import WorkerOptions, process_next, processing_settings, run_pending
from it_access.providers import MockProvider, ProviderError
from it_access.storage import Store


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/mock_responses"
REPLY = (FIXTURES / "complete-001.json").read_text()
GOOD = MockProvider(fixture_text=REPLY).complete("")


class Recovery(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.directory / "intake.db"
        self.store = Store(self.db)
        self.options = WorkerOptions(workers=2, max_attempts=3, backoff_seconds=0,
                                     max_backoff_seconds=5, requests_per_minute=0)

    def ingest(self, document_id="IT-001.json", fixture="complete-001.json"):
        settings = processing_settings(mock_reply=(FIXTURES / fixture).read_text())
        self.store.ingest(document_id, (ROOT / "materials/requests/IT-001.json").read_bytes(), settings)

    def events(self):
        return self.store.history("IT-001.json")[-1]["events"]

    def test_t21_provider_failure_is_visible_sanitized_and_other_work_completes(self):
        self.ingest()
        self.ingest("other.json")
        broken = mock.Mock()
        broken.complete.side_effect = ProviderError("secret transport credentials")
        good = MockProvider(fixture_text=REPLY)
        # Factory creation follows claim order for the single-worker test.
        providers = iter((broken, good))
        count = run_pending(self.store, options=WorkerOptions(workers=1),
                            provider_factory=lambda settings: next(providers))
        self.assertEqual(count, 2)
        queue = self.store.queue()
        self.assertEqual(queue[0]["status"], "error")
        self.assertEqual(queue[0]["next_action"], "retry-extraction")
        self.assertEqual(queue[1]["status"], "review")
        self.assertIsNone(self.store.current()[0]["extraction"])
        self.assertNotIn("secret transport credentials", json.dumps(self.store.history("IT-001.json")))
        self.assertEqual(broken.complete.call_count, 1)

    def test_t21_transient_failures_retry_within_a_bounded_claim(self):
        self.ingest()
        provider = mock.Mock()
        provider.complete.side_effect = [ProviderError("busy", retryable=True), TimeoutError("secret"), GOOD]
        process_next(self.store, options=self.options, provider_factory=lambda settings: provider)
        self.assertEqual(provider.complete.call_count, 3)
        self.assertEqual(self.store.queue()[0]["status"], "review")
        self.assertEqual(len(self.store.history("IT-001.json")), 1)
        self.assertEqual(sum(e["event"] == "provider-call" for e in self.events()), 3)

    def test_t21_timeout_budget_is_passed_to_provider_and_exhaustion_is_terminal(self):
        self.ingest()
        provider = mock.Mock()
        provider.complete.side_effect = TimeoutError("secret timeout details")
        options = WorkerOptions(workers=1, timeout_seconds=0.2, max_attempts=2, backoff_seconds=0)
        run_pending(self.store, options=options, provider_factory=lambda settings: provider)
        self.assertEqual(provider.complete.call_count, 2)
        self.assertEqual(provider.complete.call_args.kwargs["timeout_seconds"], 0.2)
        self.assertEqual(self.store.current()[0]["error"]["code"], "provider-timeout")

    def test_t21_unexpected_provider_exception_does_not_abort_the_batch(self):
        self.ingest()
        provider = mock.Mock()
        provider.complete.side_effect = RuntimeError("secret details")
        process_next(self.store, options=self.options, provider_factory=lambda settings: provider)
        self.assertEqual(self.store.queue()[0]["status"], "error")
        self.assertNotIn("secret details", json.dumps(self.events()))

    def test_t22_fast_result_can_be_inspected_while_another_call_is_blocked(self):
        self.ingest()
        self.ingest("other.json")
        blocked = threading.Event()
        release = threading.Event()
        fast_finished = threading.Event()
        calls = [0]
        lock = threading.Lock()

        class ControlledProvider:
            def complete(inner, message, *, instructions="", timeout_seconds=30):
                with lock:
                    calls[0] += 1
                    position = calls[0]
                if position == 1:
                    blocked.set()
                    if not release.wait(timeout=2):
                        raise TimeoutError
                else:
                    fast_finished.set()
                return GOOD

        thread = threading.Thread(target=run_pending, kwargs={"store": self.store,
            "options": self.options, "provider_factory": lambda settings: ControlledProvider()})
        thread.start()
        try:
            self.assertTrue(blocked.wait(1))
            self.assertTrue(fast_finished.wait(1))
            deadline = time.monotonic() + 1
            while not any(row["processing_state"] == "completed" for row in self.store.current()):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
            states = {row["processing_state"] for row in self.store.current()}
            self.assertEqual(states, {"completed", "running"})
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_t22_concurrency_never_exceeds_configured_workers(self):
        for index in range(6):
            self.ingest(f"document-{index}.json")
        active = [0]
        maximum = [0]
        lock = threading.Lock()
        rendezvous = threading.Barrier(2)

        class CountingProvider:
            def complete(inner, message, **kwargs):
                with lock:
                    active[0] += 1
                    maximum[0] = max(maximum[0], active[0])
                rendezvous.wait(timeout=2)
                with lock:
                    active[0] -= 1
                return GOOD

        self.assertEqual(run_pending(self.store, options=self.options,
                         provider_factory=lambda settings: CountingProvider()), 6)
        self.assertEqual(maximum[0], 2)

    def test_t24_expired_claim_recovery_fences_old_token_and_retains_audit(self):
        now = [100.0]
        store = Store(self.db, clock=lambda: now[0])
        self.ingest()
        old = store.claim(lease_seconds=10)
        self.assertEqual(store.recover_expired(), 0)
        now[0] = 111
        self.assertEqual(store.recover_expired(), 1)
        self.assertEqual(store.recover_expired(), 0)
        new = store.claim()
        self.assertNotEqual(new.token, old.token)
        extraction = json.loads(GOOD.output_text)
        self.assertFalse(store.complete(old, extraction, "mock"))
        self.assertTrue(store.complete(new, extraction, "mock"))
        events = store.history("IT-001.json")[0]["events"]
        self.assertEqual(sum(e["event"] == "claimed" for e in events), 2)
        self.assertEqual(sum(e["event"] == "recovered" for e in events), 1)

    def test_t24_live_claim_can_be_renewed_but_expired_claim_cannot(self):
        now = [100.0]
        store = Store(self.db, clock=lambda: now[0])
        self.ingest()
        claim = store.claim(lease_seconds=10)
        now[0] = 105
        self.assertTrue(store.renew(claim, lease_seconds=10))
        now[0] = 112
        self.assertEqual(store.recover_expired(), 0)
        now[0] = 116
        self.assertFalse(store.renew(claim, lease_seconds=10))
        self.assertEqual(store.recover_expired(), 1)

    def test_t24_heartbeat_keeps_a_slow_live_worker_owned(self):
        self.ingest()
        entered = threading.Event()
        release = threading.Event()

        class SlowProvider:
            def complete(inner, *args, **kwargs):
                entered.set()
                release.wait(2)
                return GOOD

        options = WorkerOptions(workers=1, lease_seconds=0.15)
        thread = threading.Thread(target=run_pending, kwargs={"store": self.store, "options": options,
                                 "provider_factory": lambda settings: SlowProvider()})
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            time.sleep(0.25)
            self.assertEqual(self.store.recover_expired(), 0)
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(self.store.queue()[0]["status"], "review")

    def test_t24_abrupt_process_exit_is_resumable_without_repeating_completed_work(self):
        self.ingest()
        run_pending(self.store)
        self.ingest("unfinished.json")
        program = "from it_access.storage import Store; import sys, os; s=Store(sys.argv[1]); assert s.claim(lease_seconds=.1); os._exit(0)"
        subprocess.run([sys.executable, "-c", program, str(self.db)], cwd=ROOT, check=True, timeout=5)
        time.sleep(0.15)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["resume", "--db", str(self.db)]), 0)
        self.assertEqual(json.loads(output.getvalue())["recovered"], 1)
        self.assertEqual(json.loads(output.getvalue())["processed"], 1)
        records = self.store.current()
        self.assertTrue(all(row["processing_state"] == "completed" for row in records))
        self.assertEqual(records[0]["claim_count"], 1)
        self.assertEqual(records[1]["claim_count"], 2)

    def test_t24_graceful_stop_releases_claim_and_starts_no_more_work(self):
        self.ingest()
        self.ingest("other.json")
        stop = threading.Event()

        class StopProvider:
            def complete(inner, *args, **kwargs):
                stop.set()
                return GOOD

        count = run_pending(self.store, options=WorkerOptions(workers=1), stop_event=stop,
                            provider_factory=lambda settings: StopProvider())
        self.assertEqual(count, 1)
        self.assertTrue(all(row["processing_state"] == "pending" for row in self.store.current()))
        self.assertIn("released", [e["event"] for e in self.events()])
        self.assertEqual(run_pending(self.store), 2)

    def test_t24_sigterm_releases_a_rate_waiting_cli_worker(self):
        self.ingest()
        self.ingest("other.json")
        process = subprocess.Popen([sys.executable, "-m", "it_access", "resume", "--db", str(self.db),
            "--workers", "1", "--requests-per-minute", "60", "--lease-seconds", "1"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                states = {row["processing_state"] for row in self.store.current()}
                if states == {"completed", "running"}:
                    break
                self.assertIsNone(process.poll())
                time.sleep(0.01)
            else:
                self.fail("CLI did not reach the rate-limited second job")
            process.send_signal(signal.SIGTERM)
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, error)
            self.assertTrue(json.loads(output)["interrupted"])
            self.assertEqual({r["processing_state"] for r in self.store.current()}, {"completed", "pending"})
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_t24_retry_budget_survives_interruption_and_resume(self):
        self.ingest()
        stop = threading.Event()
        interrupted = mock.Mock()

        def fail_then_stop(*args, **kwargs):
            stop.set()
            raise ProviderError("busy", retryable=True)

        interrupted.complete.side_effect = fail_then_stop
        options = WorkerOptions(workers=1, max_attempts=2, backoff_seconds=0)
        run_pending(self.store, options=options, stop_event=stop, provider_factory=lambda settings: interrupted)
        self.assertEqual(self.store.current()[0]["processing_state"], "pending")
        resumed = mock.Mock()
        resumed.complete.side_effect = TimeoutError
        run_pending(self.store, options=options, provider_factory=lambda settings: resumed)
        self.assertEqual(resumed.complete.call_count, 1)
        calls = [event["details"]["number"] for event in self.events() if event["event"] == "provider-call"]
        self.assertEqual(calls, [1, 2])

    def test_t28_manual_retry_after_success_has_new_attempt_and_failure_is_current(self):
        self.ingest()
        run_pending(self.store)
        original = self.store.current()[0]
        retried = self.store.retry("IT-001.json")
        self.assertNotEqual(retried, original["attempt_id"])
        self.assertEqual(self.store.queue()[0]["status"], "pending")
        provider = mock.Mock()
        provider.complete.side_effect = ProviderError("failure")
        run_pending(self.store, provider_factory=lambda settings: provider)
        current = self.store.current()[0]
        history = self.store.history("IT-001.json")
        self.assertEqual(current["status"], "error")
        self.assertIsNone(current["extraction"])
        self.assertEqual(history[0]["status"], "review")
        self.assertEqual(current["source_sha256"], original["source_sha256"])
        self.assertEqual(current["settings_fingerprint"], original["settings_fingerprint"])
        with self.assertRaises(ValueError):
            self.store.retry("missing.json")

    def test_t28_cli_retry_can_reproduce_failure_with_explicit_fixture(self):
        self.ingest()
        run_pending(self.store)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["retry", "IT-001.json", "--db", str(self.db),
                                   "--fixture", str(FIXTURES / "refusal.json")]), 0)
        self.assertEqual(self.store.current()[0]["error"]["code"], "refusal")
        self.assertEqual(self.store.history("IT-001.json")[0]["status"], "review")

    def test_t28_retry_rejects_pending_or_running_work(self):
        self.ingest()
        with self.assertRaises(ValueError):
            self.store.retry("IT-001.json")

    def test_t28_overlapping_manual_retries_create_only_one_new_attempt(self):
        self.ingest()
        run_pending(self.store)

        def retry():
            try:
                return Store(self.db).retry("IT-001.json")
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda item: retry(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.history("IT-001.json")), 2)
        self.store.claim()
        with self.assertRaises(ValueError):
            self.store.retry("IT-001.json")

    def test_t29_invalid_refused_or_incomplete_responses_are_not_automatically_retried(self):
        for name in ("refusal", "incomplete", "malformed", "wrong-schema"):
            self.ingest(document_id=f"{name}.json", fixture=f"{name}.json")
        provider = mock.Mock()

        def factory(settings):
            provider.complete.return_value = MockProvider(fixture_text=settings["mock_reply"]).complete("")
            return provider

        run_pending(self.store, options=WorkerOptions(workers=1), provider_factory=factory)
        self.assertEqual(provider.complete.call_count, 4)
        self.assertTrue(all(row["status"] == "error" for row in self.store.queue()))

    def test_t29_retry_after_and_exponential_backoff_use_bounded_waits(self):
        self.ingest()
        provider = mock.Mock()
        provider.complete.side_effect = [ProviderError("rate limit", retryable=True, retry_after=0.2), GOOD]
        waited = []
        now = [time.time()]
        self.store.clock = lambda: now[0]

        def advance(stop, seconds):
            waited.append(seconds)
            now[0] += seconds
            return False

        with mock.patch("it_access.intake._wait", side_effect=advance):
            process_next(self.store, options=self.options, provider_factory=lambda settings: provider)
        self.assertTrue(any(delay >= 0.2 for delay in waited))
        self.assertEqual(provider.complete.call_count, 2)

    def test_t29_exponential_backoff_and_connection_failure_recovery(self):
        self.ingest()
        provider = mock.Mock()
        provider.complete.side_effect = [ConnectionError("secret"), ProviderError("busy", retryable=True), GOOD]
        waited = []
        options = WorkerOptions(workers=1, backoff_seconds=0.25, max_backoff_seconds=1)
        with mock.patch("it_access.intake.random.uniform", return_value=1), \
             mock.patch("it_access.intake._wait", side_effect=lambda stop, seconds: waited.append(seconds) or False):
            process_next(self.store, options=options, provider_factory=lambda settings: provider)
        self.assertEqual(waited, [0.25, 0.5])
        self.assertEqual(self.store.queue()[0]["status"], "review")

    def test_t29_long_retry_after_is_reported_without_retrying_too_early(self):
        self.ingest()
        provider = mock.Mock()
        provider.complete.side_effect = ProviderError("rate limit", retryable=True, retry_after=120)
        process_next(self.store, options=self.options, provider_factory=lambda settings: provider)
        self.assertEqual(provider.complete.call_count, 1)
        self.assertEqual(self.store.current()[0]["error"]["retry_after_seconds"], 120)

    def test_t29_shared_rate_gate_spaces_calls_and_honors_a_cooldown(self):
        now = [100.0]
        first = Store(self.db, clock=lambda: now[0])
        second = Store(self.db, clock=lambda: now[0])
        self.assertEqual(first.request_delay("mock/fixture", interval_seconds=2), 0)
        self.assertEqual(second.request_delay("mock/fixture", interval_seconds=2), 2)
        second.defer_requests("mock/fixture", seconds=10)
        now[0] = 105
        self.assertEqual(first.request_delay("mock/fixture", interval_seconds=2), 5)
        now[0] = 111
        self.assertEqual(first.request_delay("mock/fixture", interval_seconds=2), 0)

    def test_t29_http_date_retry_after_is_parsed(self):
        self.ingest()
        provider = mock.Mock()
        retry_at = format_datetime(datetime.fromtimestamp(time.time() + 120, timezone.utc), usegmt=True)
        provider.complete.side_effect = ProviderError("rate limit", retryable=True, retry_after=retry_at)
        process_next(self.store, options=self.options, provider_factory=lambda settings: provider)
        self.assertEqual(provider.complete.call_count, 1)
        self.assertGreater(self.store.current()[0]["error"]["retry_after_seconds"], 115)

    def test_t29_options_reject_invalid_bounds(self):
        for kwargs in ({"workers": 0}, {"workers": 1000}, {"max_attempts": 0}, {"timeout_seconds": 0},
                       {"lease_seconds": float("nan")}, {"requests_per_minute": -1},
                       {"max_backoff_seconds": -1}, {"backoff_seconds": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                WorkerOptions(**kwargs)
