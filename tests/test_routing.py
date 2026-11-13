"""T12, T14–T20: policy order and the independently authored 20-message oracle."""
import copy
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from test_mock_provider import OfflineTestCase
from it_access.cli import main
from it_access.extraction import ExtractionError
from it_access.intake import processing_settings, run_pending
from it_access.routing import prepare_review
from it_access.storage import Store


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/mock_responses"
MAPPING = json.loads((FIXTURES / "fixture-map.json").read_text())
EXPECTED = json.loads((ROOT / "tests/expected/routing.json").read_text())


def fixture(number):
    name = f"IT-{number:03}.json"
    return json.loads(json.loads((FIXTURES / MAPPING[name]).read_text())["output_text"])


def message(number):
    return json.loads((ROOT / f"materials/requests/IT-{number:03}.json").read_text())["message"]


class ReviewRouting(OfflineTestCase):
    def review(self, number, *, changes=None, document_id=None):
        extraction = fixture(number)
        extraction.update(changes or {})
        return prepare_review(document_id or f"IT-{number:03}.json", extraction, message(number))

    def assert_route(self, result, status, owner, action):
        self.assertEqual((result["status"], result["owner"], result["next_action"]), (status, owner, action))

    def test_t12_mismatch_is_detected_without_model_issue_and_id_is_retained(self):
        result = self.review(16, changes={"issues": []})
        self.assertEqual(result["extraction"]["request_id"], "IT-061")
        self.assertTrue(any(issue.startswith("mismatch:request_id:") for issue in result["extraction"]["issues"]))
        self.assert_route(result, "needs-information", "IT Service Desk", "clarify")

    def test_t12_filename_without_an_identifier_does_not_invent_a_mismatch(self):
        result = self.review(1, document_id="followup.json")
        self.assert_route(result, "review", "System Owner", "review-request")

    def test_t14_embedded_override_goes_to_inspection_and_keeps_legitimate_facts(self):
        with mock.patch.dict(os.environ, {"API_KEY": "routing-test-secret"}):
            result = self.review(6)
        self.assert_route(result, "review", "IT Service Desk", "inspect-message")
        self.assertEqual(result["extraction"]["permission"], "read")
        self.assertEqual(result["extraction"]["environment"], "staging")
        self.assertNotIn("routing-test-secret", json.dumps(result))
        self.assertIn("SYSTEM OVERRIDE", " ".join(result["extraction"]["supporting_quotes"]))

    def test_t14_supplied_override_is_detected_even_if_model_omits_marker_and_quote(self):
        result = self.review(6, changes={"issues": [], "supporting_quotes": ["Manager is Nia Patel."]})
        self.assert_route(result, "review", "IT Service Desk", "inspect-message")
        self.assertTrue(any(issue.startswith("override:message:") for issue in result["extraction"]["issues"]))
        self.assertIn("Bypass approval rules", " ".join(result["extraction"]["supporting_quotes"]))

    def test_t14_override_precedes_withdrawal_conflicts_and_mismatch(self):
        result = self.review(6, changes={"kind": "withdrawal", "manager": None,
                            "issues": ["conflict:manager: Unresolved manager."]}, document_id="IT-999.json")
        self.assert_route(result, "review", "IT Service Desk", "inspect-message")

    def test_t15_withdrawal_needs_only_identifier_and_request_date(self):
        result = self.review(5)
        self.assertIsNone(result["extraction"]["system"])
        self.assert_route(result, "review", "IT Service Desk", "confirm-withdrawal")

    def test_t15_withdrawal_precedes_filename_mismatch_and_other_missing_fields(self):
        result = self.review(5, document_id="IT-999.json")
        self.assert_route(result, "review", "IT Service Desk", "confirm-withdrawal")
        self.assertTrue(any(issue.startswith("mismatch:") for issue in result["extraction"]["issues"]))

    def test_t15_undated_withdrawal_needs_information(self):
        result = self.review(5, changes={"request_date": None,
                            "issues": fixture(5)["issues"] + ["missing:request_date: Date absent."]})
        self.assert_route(result, "needs-information", "IT Service Desk", "clarify")

    def test_t16_each_missing_required_fact_and_unknown_kind_requires_clarification(self):
        fields = ("request_id", "request_date", "requester", "team", "system", "environment",
                  "permission", "business_purpose", "start_date", "end_date", "manager", "kind")
        for field in fields:
            with self.subTest(field=field):
                result = self.review(1, changes={field: None, "issues": [f"missing:{field}: Not stated."]})
                self.assert_route(result, "needs-information", "IT Service Desk", "clarify")

    def test_t16_conflicts_other_kind_and_missing_details_use_service_desk(self):
        for number in (3, 4, 9, 10, 11, 16, 17, 20):
            with self.subTest(number=number):
                self.assert_route(self.review(number), "needs-information", "IT Service Desk", "clarify")

    def test_t17_reversed_dates_are_preserved_and_flagged(self):
        result = self.review(14, changes={"issues": []})
        self.assert_route(result, "needs-information", "IT Service Desk", "clarify")
        self.assertEqual(result["extraction"]["start_date"], "2026-11-05")
        self.assertEqual(result["extraction"]["end_date"], "2026-11-02")
        self.assertTrue(any(i.startswith("date-order:start_date:") for i in result["extraction"]["issues"]))

    def test_t18_risk_rules_include_production_read_and_staging_write_admin(self):
        for number in (2, 7, 12, 13, 19):
            with self.subTest(number=number):
                self.assert_route(self.review(number), "review", "Security Review", "review-risk-then-owner")

    def test_t19_complete_staging_read_including_corrections_uses_system_owner(self):
        for number in (1, 8, 15, 18):
            with self.subTest(number=number):
                self.assert_route(self.review(number), "review", "System Owner", "review-request")

    def test_t20_validation_precedes_routing_and_does_not_mutate_input(self):
        original = fixture(16)
        before = copy.deepcopy(original)
        prepare_review("IT-016.json", original, message(16))
        self.assertEqual(original, before)
        with self.assertRaises(ExtractionError):
            self.review(1, changes={"approved": True})

    def test_t20_all_20_routes_match_source_derived_expectations_and_never_approve(self):
        policy = (ROOT / "materials/office-policy.md").read_text()
        self.assertEqual(len(EXPECTED), 20)
        for document_id, expected in EXPECTED.items():
            number = int(document_id[3:6])
            with self.subTest(document_id=document_id):
                self.assertIn(expected["source_quote"], message(number))
                self.assertIn(expected["policy_quote"], policy)
                result = self.review(number)
                self.assert_route(result, expected["status"], expected["owner"], expected["next_action"])
                self.assertNotEqual(result["status"], "approved")


class PersistentQueue(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.directory / "queue.db"
        self.store = Store(self.db)

    def ingest(self, number=1, **changes):
        document_id = f"IT-{number:03}.json"
        settings = processing_settings(mock_reply=(FIXTURES / MAPPING[document_id]).read_text()) | changes
        self.store.ingest(document_id, (ROOT / "materials/requests" / document_id).read_bytes(), settings)

    def test_t20_full_intake_queue_matches_records_hashes_and_routing(self):
        for number in range(1, 21):
            self.ingest(number)
        self.assertEqual(run_pending(self.store), 20)
        records = {r["document_id"]: r for r in self.store.current()}
        queue = self.store.queue()
        self.assertEqual(len(queue), 20)
        for item in queue:
            document_id = item["document_id"]
            record = records[document_id]
            with self.subTest(document_id=document_id):
                for field in ("attempt_id", "source_sha256", "settings_fingerprint", "status", "owner", "next_action"):
                    self.assertEqual(item[field], record[field])
                for field in ("status", "owner", "next_action"):
                    self.assertEqual(item[field], EXPECTED[document_id][field])
                self.assertEqual(record["processing_state"], "completed")
                self.assertEqual(item["source_sha256"], hashlib.sha256(
                    (ROOT / "materials/requests" / document_id).read_bytes()).hexdigest())
        before = self.store.queue()
        for number in range(1, 21):
            self.ingest(number)
        self.assertEqual(run_pending(self.store), 0)
        self.assertEqual(self.store.queue(), before)

    def test_t20_queue_shows_pending_and_running_without_review_decisions(self):
        self.ingest()
        pending = self.store.queue()[0]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["next_action"], "await-extraction")
        self.store.claim()
        self.assertEqual(self.store.queue()[0]["status"], "processing")

    def test_t20_new_failure_keeps_old_route_only_in_history(self):
        self.ingest()
        run_pending(self.store)
        self.ingest(mock_reply=(FIXTURES / "refusal.json").read_text())
        run_pending(self.store)
        item = self.store.queue()[0]
        self.assertEqual((item["status"], item["owner"], item["next_action"]),
                         ("error", "IT Service Desk", "retry-extraction"))
        self.assertIsNone(self.store.current()[0]["extraction"])
        self.assertEqual(self.store.history("IT-001.json")[0]["owner"], "System Owner")

    def test_t20_unsupported_policy_is_visible_error_before_provider_call(self):
        policy = processing_settings()["policy"].replace("System Owner", "Different Review Team")
        self.ingest(policy=policy)
        provider = mock.Mock()
        run_pending(self.store, provider_factory=lambda settings: provider)
        provider.complete.assert_not_called()
        self.assertEqual(self.store.queue()[0]["status"], "error")
        self.assertEqual(self.store.current()[0]["error"]["code"], "unsupported-policy")

    def test_t20_step3_database_is_preserved_and_legacy_results_require_refresh(self):
        settings = processing_settings(mock_reply=(FIXTURES / MAPPING["IT-001.json"]).read_text())
        settings["pipeline_version"] = "3.0"
        settings.pop("routing_version", None)
        self.store.ingest("IT-001.json", (ROOT / "materials/requests/IT-001.json").read_bytes(), settings)
        # Construct the completed pre-step-4 database shape, with no routing table.
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP TABLE review_routes")
            connection.execute("UPDATE attempts SET processing_state='completed',extraction_json=?,response_source='mock'",
                               (json.dumps(fixture(1)),))
        reopened = Store(self.db)
        self.assertEqual(reopened.queue()[0]["status"], "refresh-required")
        self.assertEqual(reopened.current()[0]["extraction"]["request_id"], "IT-001")
        self.ingest()
        run_pending(self.store)
        self.assertEqual(self.store.queue()[0]["owner"], "System Owner")
        self.assertEqual(len(self.store.history("IT-001.json")), 2)
        self.assertIsNone(self.store.history("IT-001.json")[0]["status"])

    def test_t20_cli_queue_uses_persisted_decisions(self):
        self.ingest()
        run_pending(self.store)
        output = io.StringIO()
        with redirect_stdout(output), mock.patch("it_access.routing.prepare_review", side_effect=AssertionError("rerouted")):
            self.assertEqual(main(["queue", "--db", str(self.db)]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["owner"], "System Owner")
