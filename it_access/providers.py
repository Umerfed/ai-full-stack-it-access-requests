"""Provider contract and deterministic, local mock implementation.

The mock replays an explicitly selected fixture regardless of message content.
It demonstrates plumbing, not language-model extraction accuracy. Response
completion and extraction schema validation belong to the extraction layer.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProviderError(Exception):
    """An actionable provider failure without raw response or credential data."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after=None, code: str = "provider-error"):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.code = code


PROVIDER_ERROR_MESSAGES = {
    "provider-error": "Provider extraction failed; inspect configuration/service availability and retry.",
    "provider-rate-limit": "Provider rate limit reached; follow the recorded cooldown and reduce the request rate.",
    "missing-api-key": "API_KEY is missing; set it in the operator environment before retrying.",
    "authentication-error": "Provider authentication failed; check API_KEY before retrying.",
    "permission-error": "Provider denied access; check the API project and model permissions.",
    "model-unavailable": "Provider model was not found; verify MODEL_ID and refresh intake settings.",
    "quota-exceeded": "Provider quota or spend limit is exhausted; resolve it before an explicit retry.",
    "invalid-provider-request": "Provider rejected the request; check model/parameter compatibility before refreshing intake.",
    "invalid-provider-response": "Provider returned an invalid response envelope; inspect the integration before retrying.",
}


@dataclass(frozen=True)
class ModelResponse:
    output_text: str
    status: str
    response_source: str
    refusal: str | None = None


class Provider(Protocol):
    def complete(self, message: str, *, instructions: str = "", timeout_seconds: float = 30) -> ModelResponse:
        """Return an envelope within the transport timeout; callers validate it."""
        ...


class MockProvider:
    """Replay a UTF-8 JSON fixture without credentials or network access."""

    def __init__(self, fixture_path: str | Path | None = None, *, fixture_text: str | None = None):
        if (fixture_path is None) == (fixture_text is None):
            raise ValueError("Supply exactly one fixture path or saved fixture text.")
        self.fixture_path = Path(fixture_path) if fixture_path is not None else None
        self.fixture_text = fixture_text

    def complete(self, message: str, *, instructions: str = "", timeout_seconds: float = 30) -> ModelResponse:
        # Message is deliberately ignored: fixtures are explicitly selected.
        try:
            raw = self.fixture_text if self.fixture_text is not None else self.fixture_path.read_text(encoding="utf-8")
        except OSError:
            raise ProviderError(
                "Cannot read mock fixture; check --fixture points to a readable file."
            ) from None
        except UnicodeError:
            raise ProviderError("Invalid mock fixture; expected UTF-8 JSON.") from None
        try:
            reply = json.loads(raw)
        except ValueError:
            raise ProviderError("Invalid mock fixture; expected a JSON object.") from None
        if (
            not isinstance(reply, dict)
            or not isinstance(reply.get("output_text"), str)
            or not isinstance(reply.get("status"), str)
            or reply.get("response_source") != "mock"
            or (reply.get("refusal") is not None and not isinstance(reply["refusal"], str))
        ):
            raise ProviderError(
                "Invalid mock fixture; expected output_text/status strings, "
                "response_source=mock and an optional string/null refusal."
            )
        return ModelResponse(
            output_text=reply["output_text"],
            status=reply["status"],
            response_source="mock",
            refusal=reply.get("refusal"),
        )
