# Northstar IT access request intake

Work in progress: steps 1–4 provide an offline mock provider, validated extraction,
persistent intake, version history and a human review queue.
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

Run persistent mock intake on all 20 supplied exports, then inspect the review
queue, current extraction records, or all attempts for a document:

```bash
python3 -m it_access intake materials/requests --fixture-map tests/mock_responses/fixture-map.json --db var/intake.db
python3 -m it_access queue --db var/intake.db
python3 -m it_access records --db var/intake.db
python3 -m it_access history IT-001.json --db var/intake.db
```

An input may be a file or directory; directory inputs use its immediate `*.json`
children. Every input filename needs an entry in the fixture map. Fixture paths
are relative to that map. The sample map covers all 20 supplied messages. A mock fixture is
explicitly assigned by filename and never infers facts from a message.

`intake` persists each message independently, then sequentially processes current
pending work in the database, including jobs prepared by other operators. Its JSON
summary reports the number of accepted inputs, their attempt IDs, and jobs claimed by this
worker (including errors); use `records` to inspect outcomes. Add `--prepare-only`
to persist jobs without processing. Repeating the intake command processes prepared
pending work and reuses unchanged completed results without another provider call.

The database stores each filename, original bytes/SHA-256, processing configuration
and fingerprint, state, extraction, response source and any error. Fingerprints
include provider/model, generation settings, prompt, policy, schema, pipeline
version and the saved mock reply. Workers use these snapshots, so changing files
after preparation cannot change an attempt's source or configuration. A changed
source or fingerprint creates a new attempt; absent files stay in the database.
The latest accepted attempt is current immediately, including pending/error work;
earlier successful results remain in history with their earlier hashes/settings.

`processing_state` is `pending`, `running`, `completed`, `error` or `superseded`.
It describes extraction work, not the human review status. Completed attempts are
retained; a newer revision supersedes unfinished older work, revoking its claim.
Unchanged errors remain visible until explicit retry support arrives in step 5.

The review queue applies the office policy in this order, after extraction
validation. Every outcome requires human action; the application grants no access.

| Condition | Status | Owner | Next action |
|---|---|---|---|
| Instruction override | review | IT Service Desk | inspect-message |
| Withdrawal with request ID and request date | review | IT Service Desk | confirm-withdrawal |
| Other/unknown kind, conflict, filename-ID mismatch, missing required fact or reversed dates | needs-information | IT Service Desk | clarify |
| Complete new/change request for production, write or admin | review | Security Review | review-risk-then-owner |
| Complete staging/read request | review | System Owner | review-request |
| Extraction failure | error | IT Service Desk | retry-extraction |

The router independently flags a stated request ID that differs from an `IT-...`
identifier in its filename, preserves both identities, and flags reversed access
dates without changing them. Conflicts and override attempts use explicit issue
markers from extraction. A source-text heuristic also detects common instruction
override wording, including the supplied attack, preserving its original text as
evidence. It may miss other phrasing or flag quoted instructions; human inspection
and the later factual accuracy check remain necessary. Manager names confer no approval.

Queue items link to their extraction by document ID, attempt ID, source hash and
settings fingerprint. Routing and annotated extraction are committed in one
transaction, and current/history records expose the same routing tuple. Queue
reads use the persisted decision and never reroute old records using today's
policy. Pending/running work appears as `pending`/`processing` with next action
`await-extraction`, separate from completed review decisions.

This implementation supports routing version `1`, pipeline `4.0`, and the supplied
office policy (SHA-256 `b949596765aef45377d9f8d0792f9113109b1f811ef4f3be26919c162e1f631a`).
Policy text changes require reviewing/updating the deterministic rules, their tests
and supported hash/version, then rerunning intake. Unknown policy text produces a
visible `unsupported-policy` error before a provider call. Changing the policy
prompt alone does not implement new business rules.

Existing step 3 databases gain the routing table without changing prior attempts.
Previously completed records without a saved route display `refresh-required` /
`refresh-intake` in the queue. Rerunning the documented intake command uses the new
pipeline fingerprint and creates routed attempts while retaining earlier history.
Prepared work from unsupported older processing versions fails visibly and also
needs fresh intake. Avoid running older application versions against an upgraded database.

Multiple operator processes can share one SQLite file **on a single machine's local
disk**. WAL permits readers while writes proceed, writes use short transactions
with a five-second busy timeout, and model calls run outside transactions.
Claim tokens and 60-second leases guard completion; an expired or superseded
worker cannot publish. Step 3 does not renew/recover expired leases or retry
provider errors automatically. If interrupted after preparation but before a
claim, rerun intake to process pending jobs; recovery of claimed work is step 5.
Do not claim support for network-filesystem storage or multiple application hosts.
Keep the database and its SQLite sidecar files together during operation; `var/`
is ignored by Git. Prefer SQLite's backup facility for a live database backup.

Step 3 evidence is in `planning/step-03-red.txt`, `planning/step-03-green.txt`, and
`planning/step-03.md`. Step 4 evidence is in `planning/step-04.md`, its red/green test
logs and its recorded mock queue. Independent expected routes and their source
quotes are in `tests/expected/routing.json`. Retry/resume, live provider configuration,
consistent deliverable exports and field-accuracy commands remain later steps.
The facilitator-provided `API_KEY`
will be used by the live integration. Do not commit credentials or environments.
