"""T04, T13, T23, T25–T27: durable versioning and shared local operation."""
import copy
import hashlib
import io
import json
import multiprocessing
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from test_mock_provider import OfflineTestCase
from it_access.cli import main
from it_access.intake import processing_settings, process_next, run_pending
from it_access.providers import MockProvider
from it_access.storage import Store


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "materials/requests/IT-001.json"
FIXTURE = ROOT / "tests/mock_responses/complete-001.json"


def shared_operator(db, source, settings, barrier):
    # fork inherits the parent's key-free, network-blocking test harness.
    barrier.wait(timeout=10)
    store = Store(db)
    store.ingest(source.name, source.read_bytes(), settings)
    run_pending(store)


class DurableIntake(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.directory / "intake.db"
        self.store = Store(self.db)
        self.settings = processing_settings(mock_reply=FIXTURE.read_text())

    def ingest(self, raw=None, settings=None, document_id="IT-001.json"):
        return self.store.ingest(document_id, SOURCE.read_bytes() if raw is None else raw,
                                 self.settings if settings is None else settings)

    def test_t04_filename_and_original_bytes_determine_identity_and_hash(self):
        raw = SOURCE.read_bytes() + b"\n"
        attempt = self.ingest(raw=raw)
        record = self.store.current()[0]
        self.assertEqual(record["attempt_id"], attempt)
        self.assertEqual(record["document_id"], "IT-001.json")
        self.assertEqual(record["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["processing_state"], "pending")

    def test_t13_same_business_id_in_two_files_keeps_two_documents(self):
        self.ingest()
        self.ingest(document_id="followup.json")
        self.assertEqual(run_pending(self.store), 2)
        records = self.store.current()
        self.assertEqual({row["document_id"] for row in records}, {"IT-001.json", "followup.json"})
        self.assertEqual([row["extraction"]["request_id"] for row in records], ["IT-001", "IT-001"])

    def test_t25_unchanged_rerun_reuses_completed_result_without_provider_call(self):
        first = self.ingest()
        provider = mock.Mock(wraps=MockProvider(FIXTURE))
        run_pending(self.store, provider_factory=lambda settings: provider)
        second = self.ingest()
        self.assertEqual(run_pending(self.store, provider_factory=lambda settings: provider), 0)
        self.assertEqual(first, second)
        self.assertEqual(provider.complete.call_count, 1)
        self.assertEqual(len(self.store.current()), 1)
        self.assertEqual(len(self.store.history("IT-001.json")), 1)

    def test_t25_pending_rerun_does_not_duplicate_jobs_and_survives_reopen(self):
        first = self.ingest()
        self.assertEqual(self.ingest(), first)
        reopened = Store(self.db)
        self.assertEqual(run_pending(reopened), 1)
        self.assertEqual(reopened.current()[0]["processing_state"], "completed")

    def test_t26_changed_source_retains_old_hash_and_extraction(self):
        first = self.ingest()
        run_pending(self.store)
        exported = json.loads(SOURCE.read_bytes())
        exported["message"] += " Correction: end date is 2026-11-07."
        raw = json.dumps(exported).encode()
        changed = copy.deepcopy(self.settings)
        fixture = json.loads(changed["mock_reply"])
        extraction = json.loads(fixture["output_text"])
        extraction["end_date"] = "2026-11-07"
        extraction["supporting_quotes"].append("Correction: end date is 2026-11-07.")
        fixture["output_text"] = json.dumps(extraction)
        changed["mock_reply"] = json.dumps(fixture)
        second = self.ingest(raw=raw, settings=changed)
        run_pending(self.store)
        history = self.store.history("IT-001.json")
        self.assertNotEqual(first, second)
        self.assertEqual([r["extraction"]["end_date"] for r in history], ["2026-11-06", "2026-11-07"])
        self.assertEqual(history[0]["source_sha256"], hashlib.sha256(SOURCE.read_bytes()).hexdigest())
        self.assertEqual(history[1]["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.store.current()[0]["attempt_id"], second)

    def test_t26_source_bytes_change_even_when_decoded_json_is_identical(self):
        first = self.ingest()
        second = self.ingest(raw=SOURCE.read_bytes() + b"\n")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.current()), 1)

    def test_t26_each_relevant_setting_creates_a_new_revision(self):
        self.ingest()
        for field, value in (
            ("provider", "test-provider"), ("model", "another-model"),
            ("generation", {"temperature": 0}), ("prompt", "New prompt"),
            ("policy", "Updated review policy"),
            ("schema", self.settings["schema"] | {"title": "Updated schema"}),
            ("pipeline_version", "next"), ("mock_reply", self.settings["mock_reply"] + "\n"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.settings)
                changed[field] = value
                before = self.store.current()[0]["attempt_id"]
                after = self.ingest(settings=changed)
                self.assertNotEqual(before, after)
                self.assertEqual(self.store.history("IT-001.json")[-1]["settings"][field], value)

    def test_t26_settings_key_order_is_irrelevant_and_snapshot_is_detached(self):
        first = self.ingest()
        reordered = dict(reversed(list(self.settings.items())))
        self.assertEqual(self.ingest(settings=reordered), first)
        self.settings["model"] = "mutated-after-ingest"
        self.assertEqual(self.store.current()[0]["settings"]["model"], "fixture")

    def test_t26_worker_uses_ingested_source_and_configuration_snapshots(self):
        self.ingest()
        with mock.patch("it_access.extraction.instructions", side_effect=AssertionError("read current config")):
            run_pending(self.store)
        record = self.store.current()[0]
        self.assertEqual(record["processing_state"], "completed")
        self.assertEqual(record["response_source"], "mock")

    def test_t26_prior_success_remains_when_source_reverts(self):
        first = self.ingest()
        run_pending(self.store)
        self.ingest(raw=SOURCE.read_bytes() + b"\n")
        third = self.ingest()
        self.assertNotEqual(first, third)
        self.assertEqual(len(self.store.history("IT-001.json")), 3)

    def test_t27_subset_batch_never_withdraws_absent_documents(self):
        self.ingest()
        self.ingest(document_id="another.json")
        self.ingest()
        self.assertEqual(len(self.store.current()), 2)

    def test_t23_claim_is_exclusive_and_completed_attempt_cannot_be_overwritten(self):
        self.ingest()
        claim = self.store.claim()
        self.assertIsNone(Store(self.db).claim())
        extraction = json.loads(json.loads(FIXTURE.read_text())["output_text"])
        self.assertTrue(self.store.complete(claim, extraction, "mock"))
        self.assertFalse(self.store.complete(claim, extraction | {"manager": "Changed"}, "mock"))
        self.assertEqual(self.store.current()[0]["extraction"]["manager"], "Priya Shah")

    def test_t23_new_revision_rejects_old_worker_completion(self):
        self.ingest()
        old_claim = self.store.claim()
        newest = self.ingest(raw=SOURCE.read_bytes() + b"\n")
        extraction = json.loads(json.loads(FIXTURE.read_text())["output_text"])
        self.assertFalse(self.store.complete(old_claim, extraction, "mock"))
        self.assertEqual(self.store.current()[0]["attempt_id"], newest)
        self.assertEqual(self.store.current()[0]["processing_state"], "pending")
        self.assertEqual(self.store.history("IT-001.json")[0]["processing_state"], "superseded")

    def test_t23_wrong_token_or_expired_claim_cannot_publish(self):
        now = [100.0]
        store = Store(self.db, clock=lambda: now[0])
        self.ingest()
        claim = store.claim(lease_seconds=10)
        extraction = json.loads(json.loads(FIXTURE.read_text())["output_text"])
        self.assertFalse(store.complete(replace(claim, token="wrong"), extraction, "mock"))
        now[0] = 111.0
        self.assertFalse(store.complete(claim, extraction, "mock"))
        self.assertEqual(store.current()[0]["processing_state"], "running")

    def test_t23_two_processes_ingest_and_process_the_same_source_once(self):
        # Use a fresh DB to exercise concurrent initialization as well as claims.
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        db = self.directory / "shared.db"
        processes = [context.Process(target=shared_operator,
                     args=(db, SOURCE, self.settings, barrier)) for _ in range(2)]
        for process in processes:
            process.start()
            self.addCleanup(lambda p=process: p.kill() if p.is_alive() else None)
        for process in processes:
            process.join(timeout=15)
            self.assertEqual(process.exitcode, 0)
        records = Store(db).current()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["processing_state"], "completed")
        self.assertEqual(records[0]["claim_count"], 1)
        self.assertEqual(len(Store(db).history("IT-001.json")), 1)

    def test_t23_failed_new_attempt_does_not_restore_older_success(self):
        self.ingest()
        run_pending(self.store)
        settings = self.settings | {"mock_reply": (ROOT / "tests/mock_responses/refusal.json").read_text()}
        self.ingest(settings=settings)
        run_pending(self.store)
        record = self.store.current()[0]
        self.assertEqual(record["processing_state"], "error")
        self.assertIsNone(record["extraction"])
        self.assertEqual(record["error"]["code"], "refusal")
        self.assertEqual(self.store.history("IT-001.json")[0]["processing_state"], "completed")

    def test_t04_cli_intake_records_and_history(self):
        mapping = self.directory / "fixtures.json"
        mapping.write_text(json.dumps({"IT-001.json": str(FIXTURE)}))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["intake", str(SOURCE), "--fixture-map", str(mapping),
                                   "--db", str(self.db)]), 0)
        self.assertEqual(json.loads(output.getvalue())["processed"], 1)
        for command in (["records", "--db", str(self.db)],
                        ["history", "IT-001.json", "--db", str(self.db)]):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(command), 0)
            self.assertEqual(json.loads(output.getvalue())[0]["extraction"]["request_id"], "IT-001")

    def test_t04_prepare_only_leaves_work_pending(self):
        mapping = self.directory / "fixtures.json"
        mapping.write_text(json.dumps({"IT-001.json": str(FIXTURE)}))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["intake", str(SOURCE), "--fixture-map", str(mapping),
                                   "--prepare-only", "--db", str(self.db)]), 0)
        self.assertEqual(self.store.current()[0]["processing_state"], "pending")
