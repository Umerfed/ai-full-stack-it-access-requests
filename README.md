# Northstar IT access request intake

Work in progress: step 1 provides an offline mock provider and CLI fixture replay.
The build sequence and requirements are in [planning/plan.md](planning/plan.md)
and [planning/spec.md](planning/spec.md); the assignment is in [CHALLENGE.md](CHALLENGE.md).

Use Python 3.12 or later. Run commands from the repository root. There are no
external dependencies at this stage, so no installation or credentials are needed.
`pyproject.toml` and `uv.lock` record the runtime and dependency baseline. If uv
is installed, `uv sync --locked --offline` prepares an isolated environment.

Replay the saved IT-001 mock response:

```bash
python3 -m it_access --help
python3 -m it_access mock-reply --fixture tests/mock_responses/complete-001.json
```

The output is a JSON response envelope labeled `response_source: "mock"`, with
`status`, `refusal`, and `output_text` (a string containing the saved extraction
JSON). The provider replays the selected fixture independently of message content.
It makes no model calls, reads no API key, and does not measure model accuracy.
Unreadable or malformed fixture files produce an error and exit code 1.

Run all automated tests without a key:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

The mock-provider tests reject network access. The initial failing run and passing
run are saved in `planning/step-01-red.txt` and `planning/step-01-green.txt`.

Intake, extraction validation, review queue, persistence, retry/resume, live
provider configuration, and accuracy commands are planned for later steps and
are not available yet. The facilitator-provided `API_KEY` will be used by the
live integration. Do not commit credentials or virtual environments.
