# Northstar IT access request intake

Work in progress: steps 1–2 provide an offline mock provider, CLI fixture replay,
and an extraction validation layer with a versioned prompt.
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

Validate a saved extraction against its original message using the Python API:

```bash
python3 - <<'PY'
import json
from pathlib import Path
from it_access.extraction import extract
from it_access.providers import MockProvider

export = json.loads(Path("materials/requests/IT-001.json").read_text())
result = extract(export, MockProvider("tests/mock_responses/complete-001.json"))
print(json.dumps(result, indent=2))
PY
```

`extract` sends only the decoded message to the provider, with trusted instructions
passed separately. The instructions combine `prompts/extraction-v1.txt`, the office
policy and the supplied schema. Completed responses must contain a schema-valid
object, calendar-valid ISO dates, exact supporting quotes and an explanatory issue
for every null scalar. Missing, relative and conflicting fields use issue prefixes
`missing:<field>:`, `relative:<field>:` and `conflict:<field>:` respectively.
Refusals, incomplete responses and invalid results raise `ExtractionError` with a
stable `code`; partial results are not returned. Provider failures use `ProviderError`.

The validator implements the vocabulary in the supplied schema rather than a
general JSON Schema engine. It verifies structure and evidence substrings, but
cannot establish whether a model interpreted every fact or conflict correctly.
Correction/change/conflict fixtures exercise the contract, not live model accuracy.
Step 2 test evidence is in `planning/step-02-red.txt` and `planning/step-02-green.txt`.

Intake, review queue, persistence, retry/resume, live provider configuration,
and accuracy commands are planned for later steps and
are not available yet. The facilitator-provided `API_KEY` will be used by the
live integration. Do not commit credentials or virtual environments.
