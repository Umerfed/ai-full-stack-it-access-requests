"""OpenAI Responses REST adapter. One HTTPS POST per recorded provider call.

No SDK, hidden retries, redirects, conversation state or tool execution. Credentials
are runtime-only. Tests replace transport/connection objects, never the extraction
validation or routing code. See planning/step-06.md for verified API references.
"""
import copy
import http.client
import json
import math
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import asdict, dataclass

from it_access.extraction import _reject_constant, _unique_object
from it_access.providers import ModelResponse, ProviderError


DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
ENDPOINT = "https://api.openai.com/v1/responses"
REQUEST_VERSION = "responses-v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
QUOTA_CODES = frozenset({
    "insufficient_quota", "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "billing_hard_limit_reached",
})
EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: dict
    body: bytes


def _invalid_response():
    return ProviderError("Invalid OpenAI response envelope.", code="invalid-provider-response")


def _decode_json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise _invalid_response() from None


def validate_configuration(settings):
    model = settings.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", model):
        raise ValueError("MODEL_ID must be a nonempty model identifier without whitespace.")
    generation = settings.get("generation")
    if not isinstance(generation, dict) or set(generation) != {"max_output_tokens", "reasoning_effort"}:
        raise ValueError("OpenAI generation settings must specify max_output_tokens and reasoning_effort only.")
    tokens = generation["max_output_tokens"]
    if type(tokens) is not int or not 1 <= tokens <= 128000:
        raise ValueError("MAX_OUTPUT_TOKENS must be an integer between 1 and 128000.")
    if not isinstance(generation["reasoning_effort"], str) or generation["reasoning_effort"] not in EFFORTS:
        raise ValueError("REASONING_EFFORT must be none, low, medium, high or xhigh.")
    if settings.get("provider_config") != {"endpoint": ENDPOINT, "request_version": REQUEST_VERSION}:
        raise ValueError("Unsupported OpenAI endpoint/request version; refresh intake with current configuration.")


def build_request(settings: dict, message: str, instructions: str) -> dict:
    validate_configuration(settings)
    return {
        "model": settings["model"],
        "instructions": instructions,
        "input": [{"role": "user", "content": message}],
        "text": {"format": {"type": "json_schema", "name": "access_request", "strict": True,
                            "schema": copy.deepcopy(settings["schema"])}},
        "max_output_tokens": settings["generation"]["max_output_tokens"],
        "reasoning": {"effort": settings["generation"]["reasoning_effort"]},
        "store": False,
        "stream": False,
    }


def post_response(payload: dict, *, api_key: str, timeout_seconds: float) -> HTTPResult:
    """TLS-verified one-shot POST with socket timeouts and a socket watchdog.

    The OS resolver is outside Python socket timeout control. Once connected, a
    deadline watchdog interrupts blocked socket I/O even if the peer trickles data.
    No redirect handling, proxy discovery or automatic retry is performed.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Transport timeout must be finite and positive.")
    deadline = time.monotonic() + timeout_seconds
    connection = http.client.HTTPSConnection(
        "api.openai.com", timeout=timeout_seconds, context=ssl.create_default_context()
    )
    watchdog = None
    expired = threading.Event()
    try:
        connection.connect()
        connected_socket = connection.sock

        def remaining():
            seconds = deadline - time.monotonic()
            if expired.is_set() or seconds <= 0:
                raise TimeoutError
            connected_socket.settimeout(seconds)
            return seconds

        def abort():
            expired.set()
            try:
                connected_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        watchdog = threading.Timer(remaining(), abort)
        watchdog.daemon = True
        watchdog.start()
        connection.request("POST", "/v1/responses",
                           body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                           headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                                    "Accept": "application/json"})
        remaining()
        response = connection.getresponse()
        chunks, size = [], 0
        while True:
            remaining()
            chunk = response.read1(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise _invalid_response()
            chunks.append(chunk)
            if response.isclosed():
                break
        if expired.is_set() or time.monotonic() > deadline:
            raise TimeoutError
        return HTTPResult(response.status, {key.lower(): value for key, value in response.getheaders()}, b"".join(chunks))
    except TimeoutError:
        raise TimeoutError("OpenAI transport timed out.") from None
    except ssl.SSLCertVerificationError:
        raise ProviderError("TLS certificate verification failed; check the local trust configuration.") from None
    except (OSError, http.client.HTTPException):
        if expired.is_set() or time.monotonic() > deadline:
            raise TimeoutError("OpenAI transport timed out.") from None
        raise ConnectionError("OpenAI transport connection failed.") from None
    finally:
        if watchdog is not None:
            watchdog.cancel()
        connection.close()


class OpenAIProvider:
    def __init__(self, settings: dict, *, api_key=None, transport=None):
        validate_configuration(settings)
        key = os.environ.get("API_KEY") if api_key is None else api_key
        if not isinstance(key, str) or not key.strip():
            raise ProviderError("API_KEY is required for OpenAI.", code="missing-api-key")
        if "\n" in key or "\r" in key:
            raise ProviderError("Invalid API_KEY configuration.", code="authentication-error")
        self._api_key = key
        self.settings = copy.deepcopy(settings)
        self._transport = transport or post_response

    def complete(self, message: str, *, instructions: str = "", timeout_seconds: float = 30) -> ModelResponse:
        request = build_request(self.settings, message, instructions)
        result = self._transport(request, api_key=self._api_key, timeout_seconds=timeout_seconds)
        if result.status != 200:
            # Only use a known error code. Never expose response messages/headers.
            try:
                body = _decode_json(result.body)
                error = body.get("error", {}) if isinstance(body, dict) else {}
                code = error.get("code") if isinstance(error, dict) else None
            except ProviderError:
                code = None
            if isinstance(code, str) and code in QUOTA_CODES:
                raise ProviderError("OpenAI quota is exhausted.", code="quota-exceeded")
            status_codes = {400: "invalid-provider-request", 401: "authentication-error", 403: "permission-error",
                            404: "model-unavailable", 429: "provider-rate-limit"}
            error_code = status_codes.get(result.status, "provider-error")
            raise ProviderError("OpenAI request failed.", code=error_code,
                                retryable=result.status in RETRYABLE_STATUSES,
                                retry_after=result.headers.get("retry-after") if result.status in RETRYABLE_STATUSES else None)
        body = _decode_json(result.body)
        if (not isinstance(body, dict) or body.get("object") != "response"
                or not isinstance(body.get("model"), str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", body["model"])
                or not isinstance(body.get("status"), str)
                or body["status"] not in {"completed", "incomplete", "failed", "cancelled", "queued", "in_progress"}):
            raise _invalid_response()
        source = f'openai/{body["model"]}'
        if body["status"] == "failed":
            error = body.get("error")
            retryable = (isinstance(error, dict) and isinstance(error.get("code"), str)
                         and error["code"] in {"server_error", "rate_limit_exceeded"})
            raise ProviderError("OpenAI response failed.", retryable=retryable)
        if body["status"] != "completed":
            return ModelResponse("", body["status"], source)
        if body.get("error") is not None or body.get("incomplete_details") is not None:
            raise _invalid_response()
        output = body.get("output")
        if not isinstance(output, list):
            raise _invalid_response()
        messages = []
        for item in output:
            if not isinstance(item, dict):
                raise _invalid_response()
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                raise _invalid_response()
            messages.append(item)
        if len(messages) != 1 or messages[0].get("status") != "completed":
            raise _invalid_response()
        content = messages[0].get("content")
        if not isinstance(content, list):
            raise _invalid_response()
        texts, refused = [], False
        for part in content:
            if not isinstance(part, dict):
                raise _invalid_response()
            if part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                refused = True
            elif part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            else:
                raise _invalid_response()
        if refused:
            return ModelResponse("", "completed", source, refusal="Provider refused extraction.")
        if not texts or not "".join(texts).strip():
            raise _invalid_response()
        return ModelResponse("".join(texts), "completed", source)


def request_evidence(settings, message, instructions, *, worker_options=None):
    """Example from the real builder; does not read credentials or call a model."""
    if worker_options is None:
        from it_access.intake import WorkerOptions
        worker_options = asdict(WorkerOptions())
    return {
        "observation": "request-example-not-sent",
        "provider": "openai",
        "request": {"method": "POST", "url": ENDPOINT,
                    "headers": {"Authorization": "Bearer [REDACTED]", "Content-Type": "application/json", "Accept": "application/json"},
                    "body": build_request(settings, message, instructions)},
        "transport": {"implementation": "Python standard-library HTTPS", "tls_verification": True,
                      "redirects": False, "automatic_retries": 0, "max_response_bytes": MAX_RESPONSE_BYTES,
                      "timeout_seconds": worker_options["timeout_seconds"],
                      "timeout_limit": "OS DNS resolution can exceed the socket deadline; connected I/O has a deadline watchdog."},
        "worker_options": worker_options,
        "response_handling": {
            "success": "Require completed response and assistant message, exactly one assistant message, no error/incomplete details, then validate extraction JSON/schema/dates/quotes/issues before routing.",
            "refusal": "Detect refusal content even with text; return no extraction and a visible refusal error.",
            "incomplete": "Non-completed status returns no usable text and becomes a visible incomplete error without automatic retry.",
            "unexpected_output": "Reject malformed/empty/ambiguous envelopes and unexpected tool output; skip reasoning items.",
            "http_retryable_statuses": sorted(RETRYABLE_STATUSES),
            "permanent_quota_codes": sorted(QUOTA_CODES),
            "errors": "Authentication/permission/model/request/quota failures are permanent; bounded transient retries and Retry-After belong to the durable scheduler; raw error messages are discarded.",
            "provenance": "Successful response_source uses openai/ plus the actual response model ID.",
        },
    }
