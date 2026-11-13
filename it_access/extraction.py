"""Validate model envelopes and extractions before they can reach the queue.

Schema checking implements the vocabulary used by snapshot.schema.json (object,
required/properties/additionalProperties, string/null, enum, and string arrays).
Semantic checks verify dates, literal quotes and null/issue consistency. They do
not prove that a model found every conflict or correctly interpreted prose.
"""
import json
import re
from datetime import date
from pathlib import Path

from it_access.providers import ModelResponse, Provider


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts/extraction-v1.txt"
SCHEMA_PATH = ROOT / "snapshot.schema.json"
POLICY_PATH = ROOT / "materials/office-policy.md"


class ExtractionError(Exception):
    """A stable error code and safe explanation; never raw model/source content."""

    def __init__(self, code: str, explanation: str):
        self.code = code
        super().__init__(explanation)


def instructions() -> str:
    """Keep trusted instructions separate from the message sent to a provider."""
    return "\n\n".join((
        PROMPT_PATH.read_text(encoding="utf-8"),
        POLICY_PATH.read_text(encoding="utf-8"),
        SCHEMA_PATH.read_text(encoding="utf-8"),
    ))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON constant")


def _check_schema(value, schema, path="extraction"):
    allowed = schema["type"]
    if isinstance(allowed, str):
        allowed = [allowed]
    types = {"object": dict, "array": list, "string": str, "null": type(None)}
    if not any(type(value) is types[name] for name in allowed):
        raise ExtractionError("invalid-schema", f"Invalid type at {path}; retry extraction.")
    if "enum" in schema and value not in schema["enum"]:
        raise ExtractionError("invalid-schema", f"Invalid enum at {path}; retry extraction.")
    if isinstance(value, dict):
        properties = schema["properties"]
        if any(field not in value for field in schema.get("required", [])):
            raise ExtractionError("invalid-schema", "Required extraction fields are missing; retry extraction.")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ExtractionError("invalid-schema", "Unexpected extraction fields; retry extraction.")
        for field in properties.keys() & value.keys():
            _check_schema(value[field], properties[field], f"{path}.{field}")
    elif isinstance(value, list):
        for item in value:
            _check_schema(item, schema["items"], path)


def validate_response(response: ModelResponse, message: str) -> dict:
    """Return a validated object, or raise without exposing partial model output."""
    if not isinstance(response, ModelResponse):
        raise ExtractionError("invalid-response", "Invalid provider envelope; check provider integration.")
    if response.refusal is not None:
        raise ExtractionError("refusal", "Provider refused extraction; inspect the message before retrying.")
    if response.status != "completed":
        raise ExtractionError("incomplete", "Provider response was not completed; retry extraction.")
    if not isinstance(response.output_text, str):
        raise ExtractionError("invalid-response", "Provider returned no text; retry extraction.")
    try:
        extraction = json.loads(
            response.output_text, object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise ExtractionError("invalid-json", "Provider returned invalid JSON; retry extraction.") from None
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    _check_schema(extraction, schema)

    for field, value in extraction.items():
        if isinstance(value, str) and not value.strip():
            raise ExtractionError("invalid-value", f"Empty {field}; use null with an issue.")
    for field in ("request_date", "start_date", "end_date"):
        value = extraction[field]
        if value is not None:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                date.fromisoformat(value)
            except ValueError:
                raise ExtractionError("invalid-date", f"Invalid {field}; use YYYY-MM-DD or null with an issue.") from None

    quotes = extraction["supporting_quotes"]
    if not quotes or any(not quote.strip() or quote not in message for quote in quotes):
        raise ExtractionError("invalid-evidence", "Supporting quotes must be nonempty exact message substrings.")
    issues = extraction["issues"]
    if any(not issue.strip() for issue in issues):
        raise ExtractionError("invalid-issues", "Issues must contain explanatory text.")
    unresolved = set()
    for issue in issues:
        parts = issue.split(":", 2)
        if parts[0] in {"missing", "relative", "conflict"}:
            if (len(parts) != 3 or parts[1] not in schema["properties"]
                    or not parts[2].strip() or extraction[parts[1]] is not None):
                raise ExtractionError("invalid-issues", "Unresolved-field issues must identify a null field and explain it.")
            unresolved.add(parts[1])
    for field, value in extraction.items():
        if value is None and field not in unresolved:
            raise ExtractionError("invalid-issues", f"Null {field} requires a field-specific explanatory issue.")
    return extraction


def extract(export: dict, provider: Provider) -> dict:
    """Extract from message alone; export metadata is never model input."""
    if (not isinstance(export, dict) or not isinstance(export.get("message"), str)
            or not export["message"].strip()):
        raise ExtractionError("invalid-input", "Export must contain a nonempty message string.")
    message = export["message"]
    response = provider.complete(message, instructions=instructions())
    return validate_response(response, message)
