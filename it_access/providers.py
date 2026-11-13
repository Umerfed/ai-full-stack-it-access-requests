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


@dataclass(frozen=True)
class ModelResponse:
    output_text: str
    status: str
    response_source: str
    refusal: str | None = None


class Provider(Protocol):
    def complete(self, message: str) -> ModelResponse:
        """Return a response envelope; callers must validate before using it."""
        ...


class MockProvider:
    """Replay a UTF-8 JSON fixture without credentials or network access."""

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)

    def complete(self, message: str) -> ModelResponse:
        # Message is deliberately ignored: fixtures are explicitly selected.
        try:
            raw = self.fixture_path.read_text(encoding="utf-8")
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
