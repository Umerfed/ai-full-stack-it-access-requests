"""Ordered, deterministic human-review routing for the supplied Northstar policy."""
import hashlib
import json
import re
from pathlib import Path

from it_access.extraction import ExtractionError, validate_response
from it_access.providers import ModelResponse


ROUTING_VERSION = "1"
PIPELINE_VERSION = "4.0"
# A policy-text change needs a reviewed routing implementation, not just a new
# prompt. Refuse unknown policies rather than applying obsolete business rules.
SUPPORTED_POLICY_SHA256 = "b949596765aef45377d9f8d0792f9113109b1f811ef4f3be26919c162e1f631a"
REQUIRED_FACTS = (
    "request_id", "request_date", "requester", "team", "system", "environment",
    "permission", "business_purpose", "start_date", "end_date", "manager",
)
OVERRIDE_PATTERN = re.compile(
    r"\bsystem\s+override\b|\bbypass\s+(?:the\s+)?approval\b|"
    r"\b(?:display|reveal|print|expose)\b[^.\n]{0,80}\bapi\s+key\b|"
    r"\bhighest[-\s]+priority\s+system\s+instruction\b|"
    r"\bignore\s+(?:all\s+)?(?:previous|system)\s+instructions\b",
    re.IGNORECASE,
)


def check_routing_settings(settings: dict) -> None:
    if (settings.get("pipeline_version") != PIPELINE_VERSION
            or settings.get("routing_version") != ROUTING_VERSION):
        raise ExtractionError("configuration-refresh-required", "Processing version is unsupported; rerun intake with current settings.")
    policy_hash = hashlib.sha256(settings["policy"].encode("utf-8")).hexdigest()
    if policy_hash != SUPPORTED_POLICY_SHA256:
        raise ExtractionError("unsupported-policy", "Policy has changed; update and verify the routing implementation before refreshing intake.")


def prepare_review(document_id: str, extraction: dict, message: str, *, schema=None) -> dict:
    """Validate, annotate independently checkable issues, then apply rules 1–5.

    Parsing a serialized copy prevents mutation of the caller's extraction.
    Override matching supplements model-supplied markers; it is a heuristic,
    not a general detector of malicious or misleading instructions.
    """
    result = validate_response(
        ModelResponse(json.dumps(extraction), "completed", "validation"), message, schema=schema
    )
    issues = result["issues"]

    def add_issue(prefix, explanation):
        if not any(issue.startswith(prefix) for issue in issues):
            issues.append(f"{prefix} {explanation}")

    filename_id = re.search(r"(?<![A-Za-z0-9])IT-\d+(?![A-Za-z0-9])", Path(document_id).stem)
    if filename_id and result["request_id"] is not None and result["request_id"] != filename_id.group():
        add_issue("mismatch:request_id:", "Message request ID differs from the filename identifier.")
    reversed_dates = (result["start_date"] is not None and result["end_date"] is not None
                      and result["start_date"] > result["end_date"])
    if reversed_dates:
        add_issue("date-order:start_date:", "Start date is after end date; clarify the access window.")

    override = bool(OVERRIDE_PATTERN.search(message)) or any(issue.startswith("override:") for issue in issues)
    if override:
        add_issue("override:message:", "Message contains an instruction-override attempt requiring inspection.")
        if message not in result["supporting_quotes"]:
            result["supporting_quotes"].append(message)

    def review(status, owner, action):
        return {"extraction": result, "status": status, "owner": owner, "next_action": action}

    # Order matters: withdrawal may qualify despite absent access facts or a
    # filename mismatch; an override still takes priority over that withdrawal.
    if override:
        return review("review", "IT Service Desk", "inspect-message")
    if result["kind"] == "withdrawal" and result["request_id"] and result["request_date"]:
        return review("review", "IT Service Desk", "confirm-withdrawal")
    clarification = (
        result["kind"] not in {"new", "change"}
        or any(result[field] is None for field in REQUIRED_FACTS)
        or any(issue.startswith(("conflict:", "mismatch:", "date-order:")) for issue in issues)
        or reversed_dates
    )
    if clarification:
        return review("needs-information", "IT Service Desk", "clarify")
    if result["environment"] == "production" or result["permission"] in {"write", "admin"}:
        return review("review", "Security Review", "review-risk-then-owner")
    if result["environment"] == "staging" and result["permission"] == "read":
        return review("review", "System Owner", "review-request")
    return review("needs-information", "IT Service Desk", "clarify")
