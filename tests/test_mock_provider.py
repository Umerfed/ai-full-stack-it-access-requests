"""T01: exercise the mock provider without credentials or network access."""
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from it_access.cli import main
from it_access.providers import MockProvider, ProviderError


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/mock_responses/complete-001.json"


class MockFoundation(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("API_KEY", None)
        self.network = self.enterContext(mock.patch.object(
            socket, "socket", side_effect=AssertionError("unexpected network access")
        ))
        self.enterContext(mock.patch.object(
            socket, "create_connection", side_effect=AssertionError("unexpected network access")
        ))

    def test_t01_saved_reply_is_returned_offline_and_labeled_mock(self):
        message = json.loads((ROOT / "materials/requests/IT-001.json").read_text())["message"]
        response = MockProvider(FIXTURE).complete(message)
        self.assertEqual(response.response_source, "mock")
        self.assertEqual(response.status, "completed")
        self.assertIsNone(response.refusal)
        self.assertEqual(response.output_text, json.loads(FIXTURE.read_text())["output_text"])
        self.assertEqual(json.loads(response.output_text)["request_id"], "IT-001")
        self.network.assert_not_called()

    def test_t01_replaying_a_fixture_does_not_extract_from_the_message(self):
        provider = MockProvider(FIXTURE)
        self.assertEqual(provider.complete("first input"), provider.complete("different input"))

    def test_t01_missing_fixture_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ProviderError, "Cannot read mock fixture"):
                MockProvider(Path(directory) / "missing.json").complete("message")

    def test_t01_invalid_fixture_has_a_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for contents in ("not JSON", "[]", '{"output_text": 1}'):
                with self.subTest(contents=contents):
                    path.write_text(contents)
                    with self.assertRaisesRegex(ProviderError, "Invalid mock fixture"):
                        MockProvider(path).complete("message")

    def test_t01_cli_can_replay_a_fixture(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["mock-reply", "--fixture", str(FIXTURE)])
        self.assertEqual(result, 0)
        reply = json.loads(output.getvalue())
        self.assertEqual(reply["response_source"], "mock")
        self.assertEqual(json.loads(reply["output_text"])["requester"], "Maya Chen")

    def test_t01_cli_missing_fixture_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stderr(output):
                result = main(["mock-reply", "--fixture", str(Path(directory) / "missing.json")])
        self.assertEqual(result, 1)
        self.assertIn("Cannot read mock fixture", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
