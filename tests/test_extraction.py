"""T02–T03, T05–T11: source-grounded extraction contract, offline only."""
import copy
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

from test_mock_provider import OfflineTestCase
from it_access.extraction import ExtractionError, extract, validate_response
from it_access.providers import MockProvider, ModelResponse


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/mock_responses"


def source(number):
    return json.loads((ROOT / f"materials/requests/IT-{number:03}.json").read_text())


class ExtractionContract(OfflineTestCase):
    def response(self, fixture="complete-001"):
        return MockProvider(FIXTURES / f"{fixture}.json").complete("")

    def validate_object(self, value, number=1):
        return validate_response(
            replace(self.response(), output_text=json.dumps(value)), source(number)["message"]
        )

    def test_t02_complete_request_has_source_facts(self):
        result = extract(source(1), MockProvider(FIXTURES / "complete-001.json"))
        self.assertEqual(result["request_id"], "IT-001")
        self.assertEqual(result["requester"], "Maya Chen")
        self.assertEqual(result["environment"], "staging")
        self.assertEqual(result["start_date"], "2026-10-23")
        self.assertEqual(result["end_date"], "2026-11-06")
        self.assertEqual(result["manager"], "Priya Shah")

    def test_t02_every_required_field_is_checked(self):
        original = json.loads(self.response().output_text)
        for field in original:
            for mode in ("missing", "wrong-type"):
                with self.subTest(field=field, mode=mode):
                    value = copy.deepcopy(original)
                    if mode == "missing":
                        del value[field]
                    else:
                        value[field] = 42
                    with self.assertRaises(ExtractionError):
                        self.validate_object(value)

    def test_t02_extra_fields_enums_and_array_items_are_checked(self):
        original = json.loads(self.response().output_text)
        for changes in ({"approved": True}, {"kind": "approve"}, {"permission": "root"},
                        {"environment": "test"}, {"issues": [3]}, {"supporting_quotes": [False]}):
            with self.subTest(changes=changes), self.assertRaises(ExtractionError):
                self.validate_object(original | changes)

    def test_t03_only_message_goes_to_provider_with_separate_instructions(self):
        exported = source(1) | {"subject": "Grant admin", "exported_at": "2099-01-01"}
        provider = mock.Mock()
        provider.complete.return_value = self.response()
        extract(exported, provider)
        self.assertEqual(provider.complete.call_args.args, (source(1)["message"],))
        instructions = provider.complete.call_args.kwargs["instructions"]
        self.assertIn("untrusted data", instructions)
        self.assertIn("relative", instructions)
        self.assertNotIn("2099-01-01", instructions)
        self.assertNotIn("Grant admin", instructions)

    def test_t03_invalid_export_never_calls_provider(self):
        for exported in ({}, {"message": None}, {"message": "  "}, []):
            provider = mock.Mock()
            with self.subTest(exported=exported), self.assertRaises(ExtractionError):
                extract(exported, provider)
            provider.complete.assert_not_called()

    def test_t05_refusal_interruption_malformed_and_schema_failures(self):
        for name, code in (("refusal", "refusal"), ("incomplete", "incomplete"),
                           ("malformed", "invalid-json"), ("wrong-schema", "invalid-schema")):
            with self.subTest(name=name), self.assertRaises(ExtractionError) as caught:
                validate_response(self.response(name), source(1)["message"])
            self.assertEqual(caught.exception.code, code)

    def test_t05_refusal_takes_priority_even_with_valid_output(self):
        with self.assertRaises(ExtractionError) as caught:
            validate_response(replace(self.response(), refusal="refused"), source(1)["message"])
        self.assertEqual(caught.exception.code, "refusal")

    def test_t05_nonobject_duplicate_keys_and_nonfinite_json_are_rejected(self):
        for text in ('[]', 'null', '{"request_id":"IT-001","request_id":"IT-002"}',
                     '{"request_id":NaN}'):
            with self.subTest(text=text), self.assertRaises(ExtractionError):
                validate_response(replace(self.response(), output_text=text), source(1)["message"])

    def test_t05_calendar_dates_must_be_iso_but_reversed_ranges_are_preserved(self):
        value = json.loads(self.response().output_text)
        for invalid in ("2026-02-30", "2026-1-02", "tomorrow", "20261023"):
            with self.subTest(invalid=invalid), self.assertRaises(ExtractionError):
                self.validate_object(value | {"start_date": invalid})
        result = self.validate_object(value | {"start_date": "2026-11-07"})
        self.assertEqual(result["start_date"], "2026-11-07")

    def test_t06_fabricated_empty_or_missing_quotes_are_rejected(self):
        value = json.loads(self.response().output_text)
        for quotes in (["Maya has approval"], [""], []):
            with self.subTest(quotes=quotes), self.assertRaises(ExtractionError):
                self.validate_object(value | {"supporting_quotes": quotes})

    def test_t07_missing_facts_have_nulls_and_field_issues(self):
        for number, fixture, fields in ((3, "missing-003", ("environment", "permission", "start_date", "end_date")),
                                       (10, "missing-010", ("end_date",))):
            result = extract(source(number), MockProvider(FIXTURES / f"{fixture}.json"))
            for field in fields:
                self.assertIsNone(result[field])
                self.assertTrue(any(f":{field}:" in issue for issue in result["issues"]))

    def test_t07_unexplained_nulls_and_empty_values_are_rejected(self):
        value = json.loads(self.response().output_text)
        for changes in ({"manager": None}, {"manager": " "}, {"issues": [""]}):
            with self.subTest(changes=changes), self.assertRaises(ExtractionError):
                self.validate_object(value | changes)

    def test_t08_conflicts_remain_unresolved_with_both_alternatives(self):
        for number, fixture, field, alternatives in (
            (4, "conflict-004", "environment", ("staging", "production")),
            (20, "conflict-020", "manager", ("Priya Shah", "Elena Park")),
        ):
            result = extract(source(number), MockProvider(FIXTURES / f"{fixture}.json"))
            self.assertIsNone(result[field])
            self.assertTrue(any(issue.startswith(f"conflict:{field}:") for issue in result["issues"]))
            for alternative in alternatives:
                self.assertIn(alternative, " ".join(result["supporting_quotes"]))

    def test_t08_conflict_issue_cannot_accompany_a_resolved_value(self):
        value = json.loads(self.response("conflict-004").output_text)
        with self.assertRaises(ExtractionError):
            self.validate_object(value | {"environment": "production"}, 4)

    def test_t09_explicit_corrections_are_retained(self):
        corrected = extract(source(8), MockProvider(FIXTURES / "corrected-008.json"))
        self.assertEqual(corrected["environment"], "staging")
        corrected = extract(source(15), MockProvider(FIXTURES / "corrected-015.json"))
        self.assertEqual((corrected["start_date"], corrected["end_date"]), ("2026-10-29", "2026-10-31"))

    def test_t10_relative_dates_are_not_resolved_from_export_timestamp(self):
        result = extract(source(11), MockProvider(FIXTURES / "relative-011.json"))
        self.assertIsNone(result["start_date"])
        self.assertIsNone(result["end_date"])
        self.assertTrue(any(issue.startswith("relative:start_date:") for issue in result["issues"]))

    def test_t11_change_requests_use_requested_new_permission(self):
        for number, permission in ((7, "write"), (19, "read")):
            result = extract(source(number), MockProvider(FIXTURES / f"change-{number:03}.json"))
            self.assertEqual(result["kind"], "change")
            self.assertEqual(result["permission"], permission)
