"""Durable intake with bounded workers, provider retries and cooperative recovery."""
import json
import math
import random
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime

from it_access.extraction import (
    ExtractionError, POLICY_PATH, PROMPT_PATH, SCHEMA_PATH, message_from_export, validate_response,
)
from it_access.providers import MockProvider, ProviderError
from it_access.routing import PIPELINE_VERSION, ROUTING_VERSION, check_routing_settings
from it_access.storage import Store


@dataclass(frozen=True)
class WorkerOptions:
    workers: int = 4
    timeout_seconds: float = 30
    max_attempts: int = 3
    lease_seconds: float = 60
    backoff_seconds: float = 1
    max_backoff_seconds: float = 30
    requests_per_minute: float = 0

    def __post_init__(self):
        if type(self.workers) is not int or not 1 <= self.workers <= 32:
            raise ValueError("workers must be an integer between 1 and 32.")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be an integer between 1 and 10.")
        for field in ("timeout_seconds", "lease_seconds", "backoff_seconds", "max_backoff_seconds", "requests_per_minute"):
            value = getattr(self, field)
            if not math.isfinite(value) or value < 0 or (field in {"timeout_seconds", "lease_seconds"} and value == 0):
                raise ValueError(f"{field} must be finite and {'positive' if field in {'timeout_seconds', 'lease_seconds'} else 'nonnegative'}.")


def processing_settings(*, provider="mock", model="fixture", generation=None, mock_reply=None) -> dict:
    """Fingerprint output-affecting inputs; credentials never belong in settings."""
    return {
        "pipeline_version": PIPELINE_VERSION,
        "routing_version": ROUTING_VERSION,
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


class _Interrupted(Exception):
    pass


class _ProviderFailure(Exception):
    def __init__(self, error):
        self.error = error


def _wait(stop, seconds):
    return stop.wait(seconds)


@contextmanager
def _heartbeat(store, claim, options):
    finished = threading.Event()
    lost = threading.Event()

    def renew():
        while not finished.wait(min(options.lease_seconds / 3, 1)):
            try:
                owned = store.renew(claim, lease_seconds=options.lease_seconds)
            except sqlite3.Error:
                owned = False
            if not owned:
                lost.set()
                return

    thread = threading.Thread(target=renew, name="it-access-lease", daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        finished.set()
        thread.join()


def _retry_after(value, now):
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def _check_stop(stop, lost):
    if stop.is_set() or lost.is_set():
        raise _Interrupted


def _call_provider(store, claim, provider, message, instructions, options, stop, lost):
    scope = f'{claim.settings["provider"]}/{claim.settings["model"]}'
    interval = 60 / options.requests_per_minute if options.requests_per_minute else 0
    prior_calls = store.provider_call_count(claim)
    if prior_calls >= options.max_attempts:
        raise _ProviderFailure({"code": "retry-budget-exhausted", "provider_calls": prior_calls,
                                "message": "This attempt has exhausted its provider-call budget; use an explicit retry after inspecting history."})
    for number in range(prior_calls + 1, options.max_attempts + 1):
        while True:
            _check_stop(stop, lost)
            delay = store.request_delay(scope, interval_seconds=interval)
            if delay == 0:
                break
            if _wait(stop, min(delay, 1)):
                raise _Interrupted
        if not store.record_event(claim, "provider-call", {"number": number, "timeout_seconds": options.timeout_seconds}):
            raise _Interrupted
        try:
            # The supported provider transport must enforce this timeout. Python
            # cannot safely preempt an arbitrary adapter blocked inside a thread.
            response = provider.complete(message, instructions=instructions, timeout_seconds=options.timeout_seconds)
            _check_stop(stop, lost)
            return response
        except _Interrupted:
            raise
        except TimeoutError:
            code, retryable, hint = "provider-timeout", True, None
        except ConnectionError:
            code, retryable, hint = "provider-unavailable", True, None
        except ProviderError as exc:
            code = "provider-rate-limit" if exc.retry_after is not None else "provider-error"
            retryable = exc.retryable
            hint = _retry_after(exc.retry_after, store.clock())
        except Exception:
            # Limit sanitization to the provider boundary, not database/program
            # failures elsewhere. Never persist arbitrary exception text.
            code, retryable, hint = "provider-error", False, None
        error = {"code": code, "message": "Provider extraction failed; inspect configuration/service availability and retry.",
                 "provider_calls": number}
        if hint is not None:
            error["retry_after_seconds"] = hint
            store.defer_requests(scope, seconds=hint)
        retry = retryable and number < options.max_attempts and (hint is None or hint <= options.max_backoff_seconds)
        delay = min(options.max_backoff_seconds, options.backoff_seconds * 2 ** (number - 1)) * random.uniform(0.5, 1)
        delay = max(delay, hint or 0)
        store.record_event(claim, "provider-failure", {"code": code, "number": number, "retry": retry,
                                                       "wait_seconds": delay if retry else 0})
        if not retry:
            raise _ProviderFailure(error)
        if _wait(stop, delay):
            raise _Interrupted
    raise AssertionError("Unreachable retry loop")


def process_next(store: Store, *, provider_factory=None, options=None, stop_event=None, document_id=None) -> bool:
    """Process one claimed job; retries stay within its durable attempt."""
    options = options or WorkerOptions()
    stop = stop_event if stop_event is not None else threading.Event()
    if stop.is_set():
        return False
    claim = store.claim(lease_seconds=options.lease_seconds, document_id=document_id)
    if claim is None:
        return False
    settings = claim.settings
    response_source = "mock" if settings["provider"] == "mock" else f'{settings["provider"]}/{settings["model"]}'
    try:
        with _heartbeat(store, claim, options) as lost:
            store.record_event(claim, "worker-options", asdict(options))
            check_routing_settings(settings)
            try:
                exported = json.loads(claim.source_bytes.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                raise ExtractionError("invalid-input", "Source must be a UTF-8 JSON export with message text.") from None
            message = message_from_export(exported)
            try:
                provider = (provider_factory or _provider)(settings)
            except Exception:
                raise _ProviderFailure({"code": "provider-error", "message": "Provider configuration failed; inspect settings and retry."}) from None
            trusted_instructions = "\n\n".join((settings["prompt"], settings["policy"], json.dumps(settings["schema"])))
            response = _call_provider(store, claim, provider, message, trusted_instructions, options, stop, lost)
            extraction = validate_response(response, message, schema=settings["schema"])
            _check_stop(stop, lost)
            store.complete(claim, extraction, response.response_source)
    except _Interrupted:
        store.release(claim)
    except ExtractionError as exc:
        store.fail(claim, {"code": exc.code, "message": str(exc)}, response_source)
    except _ProviderFailure as exc:
        store.fail(claim, exc.error, response_source)
    return True


def run_pending(store: Store, *, provider_factory=None, options=None, stop_event=None, document_id=None) -> int:
    """Drain available work with bounded threads and prompt per-document commits.

    Shared claims fence overlapping schedulers. Cooperative stop returns unfinished
    owned work to pending once its bounded provider call returns.
    """
    options = options or WorkerOptions()
    stop = stop_event if stop_event is not None else threading.Event()

    def worker():
        count = 0
        while not stop.is_set() and process_next(store, provider_factory=provider_factory, options=options,
                                                 stop_event=stop, document_id=document_id):
            count += 1
        return count

    with ThreadPoolExecutor(max_workers=options.workers, thread_name_prefix="it-access") as executor:
        futures = [executor.submit(worker) for _ in range(options.workers)]
        return sum(future.result() for future in futures)
