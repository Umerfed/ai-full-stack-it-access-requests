"""T32–T40: independent expectations, paired exports and reproducible commands."""
import copy
import hashlib
import io
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from test_mock_provider import OfflineTestCase
from it_access.accuracy import evaluate, load_expectations
from it_access.cli import main
from it_access.exports import export_snapshot, verify_export
from it_access.intake import processing_settings, run_pending
from it_access.storage import Store


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "tests/expected"
FIXTURES = ROOT / "tests/mock_responses"


class Deliverables(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.directory / "intake.db")
        self.out = self.directory / "deliverables"
        self.ledger = load_expectations(EXPECTED)

    def ingest(self, name="IT-001.json", raw=None, reply="complete-001.json"):
        raw = raw if raw is not None else (ROOT / "materials/requests" / name).read_bytes()
        self.store.ingest(name, raw, processing_settings(mock_reply=(FIXTURES / reply).read_text()))

    def records(self):
        return [json.loads(line) for line in (self.out / "extractions.jsonl").read_text().splitlines()]

    def one_expected(self, name="IT-001.json"):
        return self.ledger | {"documents": {name: self.ledger["documents"][name]}}

    def test_t32_expected_fields_are_grounded_in_21_original_sources(self):
        self.assertEqual(len(self.ledger["documents"]), 21)
        self.assertEqual(len(self.ledger["scalar_fields"]), 12)
        for name, item in self.ledger["documents"].items():
            raw = (ROOT / item["source_path"]).read_bytes()
            self.assertEqual(item["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(item["message"], json.loads(raw)["message"])
            self.assertEqual(set(item["expected"]), set(self.ledger["scalar_fields"]))
            for field, value in item["expected"].items():
                if value is None:
                    self.assertIn(field, item["null_reasons"])
                else:
                    self.assertTrue(item["evidence"][field])
                for quote in item["evidence"][field]:
                    self.assertIn(quote, item["message"])
            for quote in item["required_evidence"]:
                self.assertIn(quote, item["message"])

    def test_t32_complete_case_and_unsupported_fact_have_fixed_denominators(self):
        self.ingest()
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        records = self.records()
        self.assertEqual(evaluate(records, self.one_expected())["scalar_exact"], {"matched": 12, "total": 12})
        records[0]["extraction"]["requester"] = "Unsupported Person"
        report = evaluate(records, self.one_expected())
        self.assertEqual(report["scalar_exact"], {"matched": 11, "total": 12})
        self.assertTrue(any(f["field"] == "requester" for f in report["scalar_discrepancies"]))

    def test_t32_failure_and_missing_record_count_all_fields_even_expected_nulls(self):
        self.ingest("IT-003.json", reply="missing-003.json")
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        records = self.records()
        self.assertEqual(evaluate(records, self.one_expected("IT-003.json"))["scalar_exact"], {"matched": 12, "total": 12})
        records[0].update(extraction=None, status="error", error={"code": "provider-error"})
        self.assertEqual(evaluate(records, self.one_expected("IT-003.json"))["scalar_exact"], {"matched": 0, "total": 12})
        self.assertEqual(evaluate([], self.one_expected("IT-003.json"))["scalar_exact"], {"matched": 0, "total": 12})

    def test_t32_duplicate_records_are_rejected_instead_of_hiding_an_attempt(self):
        self.ingest()
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        records = self.records()
        with self.assertRaises(ValueError):
            evaluate(records + records, self.one_expected())

    def test_t32_wording_difference_is_reported_without_relaxing_exact_match(self):
        self.ingest()
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        records = self.records()
        records[0]["extraction"]["business_purpose"] = "checking the weekly conversion dashboard"
        report = evaluate(records, self.one_expected())
        self.assertEqual(report["scalar_exact"], {"matched": 11, "total": 12})
        self.assertEqual(report["manual_review_required"][0]["field"], "business_purpose")

    def test_t32_conflict_evidence_and_issue_coverage_are_measured_separately(self):
        self.ingest("IT-004.json", reply="conflict-004.json")
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        records = self.records()
        records[0]["extraction"]["supporting_quotes"] = ["read access in staging"]
        report = evaluate(records, self.one_expected("IT-004.json"))
        self.assertTrue(report["missing_evidence"])
        self.assertEqual(report["issue_coverage"], {"matched": 1, "total": 1})

    def test_t32_source_version_disagreement_is_an_integrity_failure(self):
        self.ingest()
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        records = self.records()
        records[0]["source_sha256"] = "0" * 64
        self.assertTrue(evaluate(records, self.one_expected())["integrity_errors"])

    def test_t32_paraphrase_is_measured_separately_and_does_not_use_export_date(self):
        self.ingest("paraphrase.json", raw=(ROOT / "tests/inputs/paraphrase.json").read_bytes(), reply="paraphrase.json")
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        report = evaluate(self.records(), self.ledger, group="paraphrase")
        self.assertEqual(report["scalar_exact"], {"matched": 12, "total": 12})
        self.assertEqual(report["documents"], 1)
        self.assertEqual(self.records()[0]["extraction"]["request_date"], "2026-10-22")

    def test_t36_export_contains_one_current_record_per_document(self):
        self.ingest()
        run_pending(self.store)
        self.ingest(raw=(ROOT / "materials/requests/IT-001.json").read_bytes() + b"\n")
        run_pending(self.store)
        summary = export_snapshot(self.store, self.out)
        records = self.records()
        self.assertEqual(summary["documents"], 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["attempt_id"], self.store.current()[0]["attempt_id"])
        self.assertEqual(len(self.store.history("IT-001.json")), 2)
        for field in ("document_id", "source_sha256", "extraction", "status", "owner", "next_action", "error", "response_source"):
            self.assertIn(field, records[0])

    def test_t37_pair_uses_one_snapshot_and_verifies_sources_and_links(self):
        self.ingest()
        run_pending(self.store)
        summary = export_snapshot(self.store, self.out)
        self.assertTrue((self.out / ".current").is_symlink())
        self.assertTrue((self.out / "queue.json").is_symlink())
        report = verify_export(self.out, sources_dir=ROOT / "materials/requests")
        self.assertEqual(report["snapshot_id"], summary["snapshot_id"])
        self.assertEqual(report["documents"], 1)
        queue = json.loads((self.out / "queue.json").read_text())
        self.assertEqual(queue["snapshot_id"], self.records()[0]["snapshot_id"])

    def test_t37_failed_publication_leaves_the_previous_pair_intact(self):
        self.ingest()
        run_pending(self.store)
        old = export_snapshot(self.store, self.out)
        self.ingest(raw=(ROOT / "materials/requests/IT-001.json").read_bytes() + b"\n")
        run_pending(self.store)
        with mock.patch("it_access.exports._publish_current", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                export_snapshot(self.store, self.out)
        self.assertEqual(verify_export(self.out)["snapshot_id"], old["snapshot_id"])
        self.assertEqual(self.records()[0]["attempt_id"], 1)

    def test_t37_pending_export_does_not_replace_an_existing_snapshot(self):
        self.ingest()
        run_pending(self.store)
        old = export_snapshot(self.store, self.out)
        self.store.retry("IT-001.json")
        with self.assertRaises(ValueError):
            export_snapshot(self.store, self.out)
        self.assertEqual(verify_export(self.out)["snapshot_id"], old["snapshot_id"])

    def test_t37_tampering_is_detected_by_manifest_hashes(self):
        self.ingest()
        run_pending(self.store)
        export_snapshot(self.store, self.out)
        (self.out / "queue.json").write_text('{"items": []}')
        with self.assertRaises(ValueError):
            verify_export(self.out)

    def test_t37_overlapping_exporters_publish_a_complete_pair(self):
        self.ingest()
        run_pending(self.store)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda item: export_snapshot(self.store, self.out), range(2)))
        self.assertEqual(results[0]["snapshot_id"], results[1]["snapshot_id"])
        self.assertEqual(verify_export(self.out)["documents"], 1)

    def test_t34_export_verify_and_accuracy_cli_commands(self):
        self.ingest()
        run_pending(self.store)
        report_path = self.directory / "accuracy.json"
        for command in (
            ["export", "--db", str(self.store.path), "--out", str(self.out)],
            ["verify-export", "--out", str(self.out), "--sources", str(ROOT / "materials/requests")],
            ["accuracy", "--records", str(self.out / "extractions.jsonl"), "--expected", str(EXPECTED), "--out", str(report_path)],
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(command), 0)
        # Nineteen missing documents still occupy the full original denominator.
        self.assertEqual(json.loads(report_path.read_text())["scalar_exact"], {"matched": 12, "total": 240})
