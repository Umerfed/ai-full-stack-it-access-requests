"""T30/T31: documented REST contract, mocked transport, no real credentials."""
import copy
import io
import json
import os
import ssl
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_mock_provider import OfflineTestCase
from it_access.cli import main
from it_access.extraction import ExtractionError, instructions, validate_response
from it_access.intake import WorkerOptions, processing_settings, run_pending
from it_access.openai_provider import (
    DEFAULT_MODEL, ENDPOINT, HTTPResult, OpenAIProvider, build_request,
    post_response, request_evidence,
)
from it_access.providers import ProviderError
from it_access.storage import Store


ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "tests/mock_responses/openai"
EXPORT = json.loads((ROOT / "materials/requests/IT-001.json").read_text())
SECRET = "unit-test-sentinel-not-a-real-key"


def wire(name="success", status=200, headers=None):
    return HTTPResult(status, headers or {}, (WIRE / f"{name}.json").read_bytes())


class OpenAIContract(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.settings = processing_settings(provider="openai")
        self.transport = mock.Mock(return_value=wire())
        self.provider = OpenAIProvider(self.settings, api_key=SECRET, transport=self.transport)

    def test_t30_request_uses_snapshot_model_schema_and_separate_message(self):
        request = build_request(self.settings, EXPORT["message"], instructions())
        self.assertEqual(DEFAULT_MODEL, "gpt-5.4-mini-2026-03-17")
        self.assertEqual(request["model"], DEFAULT_MODEL)
        self.assertEqual(request["input"], [{"role": "user", "content": EXPORT["message"]}])
        self.assertEqual(request["instructions"], instructions())
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "access_request", "strict": True,
            "schema": json.loads((ROOT / "snapshot.schema.json").read_text()),
        })
        self.assertEqual(request["max_output_tokens"], 4096)
        self.assertEqual(request["reasoning"], {"effort": "none"})
        self.assertIs(request["store"], False)
        self.assertIs(request["stream"], False)
        self.assertNotIn("temperature", request)
        self.assertNotIn("tools", request)

    def test_t30_success_uses_actual_response_model_and_validated_text(self):
        response = self.provider.complete(EXPORT["message"], instructions=instructions(), timeout_seconds=2.5)
        self.assertEqual(response.response_source, f"openai/{DEFAULT_MODEL}")
        self.assertEqual(validate_response(response, EXPORT["message"])["request_id"], "IT-001")
        self.assertEqual(self.transport.call_args.kwargs["timeout_seconds"], 2.5)
        self.assertEqual(self.transport.call_args.kwargs["api_key"], SECRET)
        self.assertEqual(self.transport.call_count, 1)

    def test_t30_actual_model_id_is_not_replaced_by_requested_alias(self):
        settings = processing_settings(provider="openai", model="gpt-5.4-mini")
        provider = OpenAIProvider(settings, api_key=SECRET, transport=self.transport)
        self.assertEqual(provider.complete(EXPORT["message"]).response_source, f"openai/{DEFAULT_MODEL}")

    def test_t30_refusal_and_incomplete_are_not_usable_extractions(self):
        for name, code in (("refusal", "refusal"), ("incomplete", "incomplete")):
            self.transport.return_value = wire(name)
            response = self.provider.complete(EXPORT["message"])
            with self.subTest(name=name), self.assertRaises(ExtractionError) as caught:
                validate_response(response, EXPORT["message"])
            self.assertEqual(caught.exception.code, code)

    def test_t30_refusal_wins_even_when_text_is_also_present(self):
        body = json.loads(wire().body)
        body["output"][0]["content"].append({"type": "refusal", "refusal": "Refused"})
        self.transport.return_value = HTTPResult(200, {}, json.dumps(body).encode())
        response = self.provider.complete(EXPORT["message"])
        self.assertIsNotNone(response.refusal)
        with self.assertRaises(ExtractionError):
            validate_response(response, EXPORT["message"])

    def test_t30_reasoning_items_are_ignored_but_tool_output_is_not_used_as_text(self):
        body = json.loads(wire().body)
        body["output"].insert(0, {"type": "reasoning", "id": "rs_mock", "summary": []})
        self.transport.return_value = HTTPResult(200, {}, json.dumps(body).encode())
        self.assertEqual(validate_response(self.provider.complete(EXPORT["message"]), EXPORT["message"])["request_id"], "IT-001")
        body["output"] = [{"type": "function_call", "arguments": "{}", "name": "fake"}]
        self.transport.return_value = HTTPResult(200, {}, json.dumps(body).encode())
        with self.assertRaises(ProviderError):
            self.provider.complete(EXPORT["message"])

    def test_t30_malformed_or_ambiguous_envelopes_fail_without_partial_data(self):
        good = json.loads(wire().body)
        bodies = [b"not JSON", b"[]", json.dumps(good | {"model": None}).encode(),
                  json.dumps(good | {"output": []}).encode(),
                  json.dumps(good | {"output": [good["output"][0], good["output"][0]]}).encode(),
                  json.dumps(good | {"error": {"code": "server_error"}}).encode(),
                  json.dumps(good | {"incomplete_details": {"reason": "max_output_tokens"}}).encode()]
        for body in bodies:
            with self.subTest(body=body[:40]), self.assertRaises(ProviderError):
                self.transport.return_value = HTTPResult(200, {}, body)
                self.provider.complete(EXPORT["message"])

    def test_t30_failed_response_is_classified_as_retryable_service_error(self):
        self.transport.return_value = wire("failed")
        with self.assertRaises(ProviderError) as caught:
            self.provider.complete(EXPORT["message"])
        self.assertTrue(caught.exception.retryable)

    def test_t30_transient_http_statuses_preserve_retry_after(self):
        for status in (408, 409, 429, 500, 502, 503, 504):
            self.transport.return_value = wire("rate-limit", status, {"retry-after": "2"})
            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                self.provider.complete(EXPORT["message"])
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(caught.exception.retry_after, "2")

    def test_t30_auth_request_and_quota_errors_are_permanent(self):
        for status, name, code in ((400, "rate-limit", "invalid-provider-request"),
                                   (401, "rate-limit", "authentication-error"),
                                   (403, "rate-limit", "permission-error"),
                                   (404, "rate-limit", "model-unavailable"),
                                   (429, "quota", "quota-exceeded")):
            self.transport.return_value = wire(name, status)
            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                self.provider.complete(EXPORT["message"])
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.code, code)

    def test_t31_missing_key_is_safe_and_never_uses_transport(self):
        with self.assertRaises(ProviderError) as caught:
            OpenAIProvider(self.settings, transport=self.transport)
        self.assertEqual(caught.exception.code, "missing-api-key")
        self.transport.assert_not_called()

    def test_t31_raw_http_error_messages_are_never_in_exceptions(self):
        body = {"error": {"message": SECRET, "code": "unknown_secret_code_" + SECRET}}
        self.transport.return_value = HTTPResult(401, {}, json.dumps(body).encode())
        with self.assertRaises(ProviderError) as caught:
            self.provider.complete(EXPORT["message"])
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, caught.exception.code)

    def test_t31_evidence_is_the_actual_request_builder_without_credentials(self):
        with mock.patch.dict(os.environ, {"API_KEY": SECRET}):
            evidence = request_evidence(self.settings, EXPORT["message"], instructions())
        self.assertEqual(evidence["observation"], "request-example-not-sent")
        self.assertEqual(evidence["request"]["url"], ENDPOINT)
        self.assertEqual(evidence["request"]["body"], build_request(self.settings, EXPORT["message"], instructions()))
        self.assertEqual(evidence["request"]["headers"]["Authorization"], "Bearer [REDACTED]")
        self.assertEqual(evidence["transport"]["automatic_retries"], 0)
        self.assertIn("response_handling", evidence)
        self.assertNotIn(SECRET, json.dumps(evidence))


class HTTPTransport(OfflineTestCase):
    def connection(self, chunks=None, status=200):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status = status
        response.getheaders.return_value = [("Retry-After", "2")]
        response.read1.side_effect = chunks if chunks is not None else [wire().body, b""]
        return connection

    def test_t30_https_post_verifies_tls_sends_auth_once_and_enforces_socket_timeout(self):
        connection = self.connection()
        with mock.patch("it_access.openai_provider.http.client.HTTPSConnection", return_value=connection) as factory:
            result = post_response({"model": DEFAULT_MODEL}, api_key=SECRET, timeout_seconds=2.5)
        self.assertEqual(factory.call_args.args, ("api.openai.com",))
        self.assertEqual(factory.call_args.kwargs["timeout"], 2.5)
        self.assertEqual(factory.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(factory.call_args.kwargs["context"].check_hostname)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", "/v1/responses"))
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(result.headers["retry-after"], "2")
        self.assertTrue(connection.sock.settimeout.called)
        connection.close.assert_called_once()

    def test_t30_timeout_is_mapped_without_internal_retry_and_connection_is_closed(self):
        connection = self.connection()
        connection.connect.side_effect = TimeoutError("secret details")
        with mock.patch("it_access.openai_provider.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(TimeoutError) as caught:
                post_response({}, api_key=SECRET, timeout_seconds=0.01)
        self.assertNotIn("secret details", str(caught.exception))
        connection.connect.assert_called_once()
        connection.close.assert_called_once()

    def test_t30_redirect_is_returned_once_and_never_followed(self):
        connection = self.connection(chunks=[b""], status=302)
        with mock.patch("it_access.openai_provider.http.client.HTTPSConnection", return_value=connection) as factory:
            result = post_response({}, api_key=SECRET, timeout_seconds=1)
        self.assertEqual(result.status, 302)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(connection.request.call_count, 1)

    def test_t30_oversized_body_is_rejected_and_connection_is_closed(self):
        connection = self.connection(chunks=[b"x" * (2 * 1024 * 1024 + 1)])
        with mock.patch("it_access.openai_provider.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(ProviderError):
                post_response({}, api_key=SECRET, timeout_seconds=1)
        connection.close.assert_called_once()


class LiveIntegration(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.directory / "intake.db"
        self.store = Store(self.db)

    def test_t30_openai_intake_works_through_mocked_wire_and_records_actual_model(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"API_KEY": SECRET}), \
             mock.patch("it_access.openai_provider.post_response", return_value=wire()) as transport, redirect_stdout(output):
            self.assertEqual(main(["intake", str(ROOT / "materials/requests/IT-001.json"),
                                   "--provider", "openai", "--db", str(self.db)]), 0)
        self.assertEqual(transport.call_count, 1)
        record = self.store.current()[0]
        self.assertEqual(record["response_source"], f"openai/{DEFAULT_MODEL}")
        self.assertEqual(record["owner"], "System Owner")
        self.assertNotIn(SECRET, json.dumps(self.store.history("IT-001.json")))
        self.assertNotIn(SECRET, output.getvalue())

    def test_t30_environment_defaults_are_snapshotted_and_resume_uses_saved_model(self):
        with mock.patch.dict(os.environ, {"MODEL_ID": "gpt-5.4-mini", "MAX_OUTPUT_TOKENS": "2048", "REASONING_EFFORT": "low"}), \
             redirect_stdout(io.StringIO()):
            main(["intake", str(ROOT / "materials/requests/IT-001.json"), "--provider", "openai",
                  "--prepare-only", "--db", str(self.db)])
        settings = self.store.current()[0]["settings"]
        self.assertEqual(settings["model"], "gpt-5.4-mini")
        self.assertEqual(settings["generation"], {"max_output_tokens": 2048, "reasoning_effort": "low"})
        with mock.patch.dict(os.environ, {"API_KEY": SECRET, "MODEL_ID": "different"}), \
             mock.patch("it_access.openai_provider.post_response", return_value=wire()) as transport, \
             redirect_stdout(io.StringIO()):
            main(["resume", "--db", str(self.db)])
        self.assertEqual(transport.call_args.args[0]["model"], "gpt-5.4-mini")

    def test_t31_missing_key_is_a_visible_error_and_does_not_call_network(self):
        with redirect_stdout(io.StringIO()), mock.patch("it_access.openai_provider.post_response") as transport:
            main(["intake", str(ROOT / "materials/requests/IT-001.json"), "--provider", "openai", "--db", str(self.db)])
        transport.assert_not_called()
        self.assertEqual(self.store.current()[0]["error"]["code"], "missing-api-key")
        self.assertIsNone(self.store.current()[0]["extraction"])

    def test_t30_transient_http_error_is_retried_only_by_durable_scheduler(self):
        settings = processing_settings(provider="openai")
        self.store.ingest("IT-001.json", (ROOT / "materials/requests/IT-001.json").read_bytes(), settings)
        with mock.patch.dict(os.environ, {"API_KEY": SECRET}), \
             mock.patch("it_access.openai_provider.post_response", side_effect=[wire("rate-limit", 503), wire()]) as transport:
            run_pending(self.store, options=WorkerOptions(workers=1, backoff_seconds=0))
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(self.store.queue()[0]["status"], "review")

    def test_t31_request_evidence_command_needs_no_key_or_network(self):
        target = self.directory / "request-evidence.json"
        with redirect_stdout(io.StringIO()), mock.patch("it_access.openai_provider.post_response") as transport:
            result = main(["request-evidence", str(ROOT / "materials/requests/IT-001.json"), "--out", str(target)])
        self.assertEqual(result, 0)
        transport.assert_not_called()
        evidence = json.loads(target.read_text())
        self.assertEqual(evidence["request"]["body"]["model"], DEFAULT_MODEL)
        self.assertEqual(evidence["observation"], "request-example-not-sent")

    def test_t31_invalid_generation_options_fail_before_creating_attempts(self):
        for options in (["--max-output-tokens", "0"], ["--reasoning-effort", "invalid"], ["--model", "bad model"]):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                result = main(["intake", str(ROOT / "materials/requests/IT-001.json"), "--provider", "openai",
                               "--db", str(self.db), *options])
            self.assertNotEqual(result, 0)
        self.assertEqual(self.store.current(), [])
