"""Source-derived exact-field comparison; never creates expectations from output."""
import json
from collections import Counter
from pathlib import Path

from it_access.extraction import ExtractionError, validate_response
from it_access.providers import ModelResponse


def load_expectations(path: str | Path) -> dict:
    path = Path(path)
    if path.is_dir():
        path = path / "fields.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("version") != 1 or len(ledger.get("scalar_fields", [])) != 12:
        raise ValueError("Expected a version-1 source-derived field ledger with 12 scalar fields.")
    return ledger


def evaluate(records: list[dict], ledger: dict, *, group="originals") -> dict:
    expected = {key: item for key, item in ledger["documents"].items() if item["group"] == group}
    if not expected:
        raise ValueError("No expectations found for the selected group.")
    indexed = {}
    for record in records:
        name = record.get("document_id")
        if not isinstance(name, str) or name in indexed:
            raise ValueError("Records must have unique document_id strings; duplicate attempts cannot be scored as current.")
        indexed[name] = record
    fields = ledger["scalar_fields"]
    report = {
        "group": group, "documents": len(expected),
        "scalar_exact": {"matched": 0, "total": len(expected) * len(fields)},
        "routing_exact": {"matched": 0, "total": len(expected)},
        "quote_validity": {"matched": 0, "total": 0},
        "evidence_coverage": {"matched": 0, "total": 0},
        "issue_coverage": {"matched": 0, "total": 0},
        "response_sources": dict(Counter(row.get("response_source", "unknown") for name, row in indexed.items() if name in expected)),
        "integrity_errors": [], "record_errors": [], "scalar_discrepancies": [],
        "manual_review_required": [], "missing_evidence": [], "missing_issues": [],
        "missing_documents": sorted(set(expected) - set(indexed)),
        "unexpected_documents": sorted(set(indexed) - set(expected)),
        "interpretation": "Exact comparison of saved outputs against fixed source-derived expectations. Mock agreement does not measure model accuracy; business-purpose differences and free-text factual claims need source review.",
    }
    for name in report["unexpected_documents"]:
        report["integrity_errors"].append({"document_id": name, "reason": "unexpected-document"})
    for name, item in expected.items():
        record = indexed.get(name)
        extraction = record.get("extraction") if record else None
        valid = isinstance(extraction, dict) and record.get("status") in {"review", "needs-information"}
        if record is not None and record.get("source_sha256") != item["source_sha256"]:
            report["integrity_errors"].append({"document_id": name, "reason": "source-version-mismatch"})
        if valid:
            try:
                validate_response(ModelResponse(json.dumps(extraction), "completed", record.get("response_source", "unknown")), item["message"])
            except ExtractionError as exc:
                valid = False
                report["record_errors"].append({"document_id": name, "code": exc.code})
        else:
            report["record_errors"].append({"document_id": name, "code": "missing-or-failed-extraction"})
        for field in fields:
            actual = extraction.get(field) if isinstance(extraction, dict) else None
            matched = valid and field in extraction and type(actual) is type(item["expected"][field]) and actual == item["expected"][field]
            report["scalar_exact"]["matched"] += int(matched)
            if not matched:
                discrepancy = {"document_id": name, "field": field, "expected": item["expected"][field],
                               "actual": actual, "usable_extraction": valid}
                report["scalar_discrepancies"].append(discrepancy)
                if field == "business_purpose" and valid:
                    report["manual_review_required"].append(discrepancy | {
                        "actual_is_source_substring": isinstance(actual, str) and actual in item["message"],
                        "reason": "Wording differs; inspect semantic equivalence without changing the exact score.",
                    })
        if record is not None and all(record.get(field) == value for field, value in item["routing"].items()):
            report["routing_exact"]["matched"] += 1
        quotes = extraction.get("supporting_quotes", []) if isinstance(extraction, dict) else []
        quotes = quotes if isinstance(quotes, list) else []
        for quote in quotes:
            report["quote_validity"]["total"] += 1
            report["quote_validity"]["matched"] += int(isinstance(quote, str) and bool(quote.strip()) and quote in item["message"])
        spans = [(field, quote) for field, values in item["evidence"].items() for quote in values]
        spans += [("policy-evidence", quote) for quote in item["required_evidence"]]
        for field, span in spans:
            covered = any(isinstance(quote, str) and span in quote for quote in quotes)
            report["evidence_coverage"]["total"] += 1
            report["evidence_coverage"]["matched"] += int(covered)
            if not covered:
                report["missing_evidence"].append({"document_id": name, "field": field, "quote": span})
        issues = extraction.get("issues", []) if isinstance(extraction, dict) else []
        issues = issues if isinstance(issues, list) else []
        for prefix in item["required_issue_prefixes"]:
            covered = any(isinstance(issue, str) and issue.startswith(prefix) and issue[len(prefix):].strip() for issue in issues)
            report["issue_coverage"]["total"] += 1
            report["issue_coverage"]["matched"] += int(bool(covered))
            if not covered:
                report["missing_issues"].append({"document_id": name, "prefix": prefix})
    return report
