"""Snapshot processing settings, prepare intake, and process durable jobs."""
import json

from it_access.extraction import (
    ExtractionError, POLICY_PATH, PROMPT_PATH, SCHEMA_PATH, message_from_export, validate_response,
)
from it_access.providers import MockProvider, ProviderError
from it_access.storage import Store


def processing_settings(*, provider="mock", model="fixture", generation=None, mock_reply=None) -> dict:
    """Fingerprint inputs only; credentials must never be included in settings."""
    return {
        "pipeline_version": "3.0",
        "provider": provider,
        "model": model,
        "generation": {} if generation is None else generation,
        "prompt": PROMPT_PATH.read_text(encoding="utf-8"),
        "policy": POLICY_PATH.read_text(encoding="utf-8"),
        "schema": json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
        "mock_reply": mock_reply,
    }


def _provider(settings):
    if settings["provider"] != "mock" or not isinstance(settings["mock_reply"], str):
        raise ProviderError("No supported provider response configured for this attempt.")
    return MockProvider(fixture_text=settings["mock_reply"])


def process_next(store: Store, *, provider_factory=None) -> bool:
    """Process one claim outside a transaction; True means a job was claimed.

    Publication can be rejected if a newer source arrives or the lease expires.
    This initial worker is sequential, with no automatic retry or lease renewal.
    """
    claim = store.claim()
    if claim is None:
        return False
    settings = claim.settings
    response_source = "mock" if settings["provider"] == "mock" else f'{settings["provider"]}/{settings["model"]}'
    try:
        try:
            exported = json.loads(claim.source_bytes.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise ExtractionError("invalid-input", "Source must be a UTF-8 JSON export with message text.") from None
        message = message_from_export(exported)
        provider = (provider_factory or _provider)(settings)
        trusted_instructions = "\n\n".join((settings["prompt"], settings["policy"], json.dumps(settings["schema"])))
        response = provider.complete(message, instructions=trusted_instructions)
        extraction = validate_response(response, message, schema=settings["schema"])
        store.complete(claim, extraction, response.response_source)
    except ExtractionError as exc:
        store.fail(claim, {"code": exc.code, "message": str(exc)}, response_source)
    except ProviderError:
        # Provider exceptions can contain transport details; do not persist them.
        store.fail(claim, {"code": "provider-error", "message": "Provider failed; check its configuration and retry extraction."}, response_source)
    return True


def run_pending(store: Store, *, provider_factory=None) -> int:
    count = 0
    while process_next(store, provider_factory=provider_factory):
        count += 1
    return count
