# Northstar IT access request intake

Work in progress: steps 1–6 provide mock/OpenAI providers, validated extraction,
persistent intake, a human review queue, concurrent workers and recovery commands.
The build sequence and requirements are in [planning/plan.md](planning/plan.md)
and [planning/spec.md](planning/spec.md); the assignment is in [CHALLENGE.md](CHALLENGE.md).

Use Python 3.12 or later with SQLite JSON functions. Run commands from the repository root. There are no
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

`intake` persists each message independently, then concurrently processes current
pending work for the selected provider, including jobs prepared by other operators. Its JSON
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
Unchanged errors remain visible until an explicit retry creates a new attempt.

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
`await-extraction`, separate from completed review decisions. Expired running
claims display `interrupted` with `resume-processing`; queue items include lease
expiry and claim count for handover.

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
Claim tokens and renewable leases guard completion; an expired or superseded
worker cannot publish. A heartbeat renews active work while model calls, rate
waits and backoff are in progress. Recovery only requeues expired current claims.
Do not claim support for network-filesystem storage or multiple application hosts.
Keep the database and its SQLite sidecar files together during operation; `var/`
is ignored by Git. Prefer SQLite's backup facility for a live database backup.

Resume pending work and recover claims left by a stopped process:

```bash
python3 -m it_access resume --db var/intake.db --workers 4
```

`resume` checks for expired claims once at startup, then drains available work.
By default it uses each saved attempt's provider. Add `--provider mock` or
`--provider openai` to restrict which pending jobs it processes.
It reuses completed results and leaves finalized errors for explicit retry. Live
claims stay with their owners. After an abrupt exit, wait for the recorded lease
to expire and run `resume` again if an initial command reports unfinished work.

Create a new attempt for a completed or failed document, keeping the previous
result and its source/configuration in history:

```bash
python3 -m it_access retry IT-010.json --db var/intake.db
python3 -m it_access history IT-010.json --db var/intake.db
```

Retry uses the current attempt's saved source/configuration; use fresh `intake`
for changed source files or model/policy settings. Pending/running documents must
use `resume`, and overlapping retry commands cannot create duplicate pending
attempts. `retry --prepare-only` creates the new attempt without running it;
otherwise retry processes only that document. The new attempt is current
immediately; a failed retry shows error/null extraction while earlier success
stays in history. For reproducible mock failure/success examples, `retry` also
accepts `--fixture tests/mock_responses/refusal.json` or an explicit success fixture.

Intake, resume and retry accept these worker controls:

| Flag | Default | Meaning |
|---|---:|---|
| `--workers` | 4 | Concurrent jobs per process; allowed range 1–32 |
| `--timeout-seconds` | 30 | Timeout passed to each provider transport |
| `--max-attempts` | 3 | Provider-call budget for one durable attempt, including calls before a resume; range 1–10 |
| `--lease-seconds` | 60 | Renewable ownership lease |
| `--backoff-seconds` | 1 | Initial exponential retry delay, with jitter |
| `--max-backoff-seconds` | 30 | Maximum automatic retry wait |
| `--requests-per-minute` | 0 | Shared provider/model spacing in this database; 0 disables spacing |

Timeouts/leases must be finite and positive; backoff/rate values must be finite
and nonnegative. Use leases comfortably longer than local database lock waits
and scheduling pauses. All operators sharing a quota should use the same rate
setting. This coordinates this database's workers only, not other internal tools
using the provider. In the current mock-only setup no network quota is consumed.

Provider timeouts, connection failures and explicitly retryable provider errors
use bounded exponential backoff with jitter. `Retry-After` accepts seconds or an
HTTP date and defers calls sharing the provider/model scope. A hint longer than
the automatic wait budget produces a visible error with `retry_after_seconds`;
it does not retry earlier than requested. Other queued calls still honor that
shared cooldown, with interruptible waits. Refusals, incomplete responses, invalid
JSON/schema/evidence and permanent provider errors require explicit follow-up.

Provider-call reservations are recorded before invocation and count toward the
durable attempt budget across restarts; a crash in that interval can consume a
call slot without sending a request. A crash after sending may require another
call on recovery; exactly-once external requests are not guaranteed. Explicit
retry starts a fresh attempt/budget. Worker options, calls/failure codes, claims,
recovery, release and final outcomes appear in each attempt's `history` events.
Raw provider exception messages, credentials and claim tokens are excluded.
Older database records have no retroactively invented event history.

Ctrl+C/SIGINT and SIGTERM stop further work cooperatively, interrupt rate/backoff
waits, and return unfinished owned work to pending after the current provider call
returns. The command reports `interrupted`/`unfinished` and exits 130/143. Provider
adapters must enforce their transport timeout: Python worker threads cannot safely
preempt arbitrary blocking adapter code. Mock calls return immediately. The OpenAI
transport applies socket timeouts and a watchdog after connection; OS DNS lookup
can exceed the requested deadline. Process kills are recovered by
lease expiry instead of the signal handler. Normal commands exit 0 even if some
extractions produced error records; inspect the queue. Invalid commands/config or
database errors exit nonzero. Unrecoverable infrastructure errors leave claims
visible for later recovery.

OpenAI integration uses a direct TLS-verified POST to
`https://api.openai.com/v1/responses`. No SDK or extra package installation is
required, and the transport performs no automatic HTTP retries or redirects.
The documented default is the pinned `gpt-5.4-mini-2026-03-17` snapshot with
`reasoning.effort=none`, 4096 output tokens, strict JSON Schema output, `store=false`
and `stream=false`. The model supports Responses and structured output.
[Official model documentation](https://developers.openai.com/api/docs/models/gpt-5.4-mini)

For an optional live intake, have the facilitator supply `API_KEY` in your
environment, then explicitly select OpenAI and a separate database:

```bash
python3 -m it_access intake materials/requests --provider openai --db var/live.db --workers 2 --requests-per-minute 30
python3 -m it_access queue --db var/live.db
python3 -m it_access resume --provider openai --db var/live.db
python3 -m it_access retry IT-010.json --db var/live.db
```

The shown rate is an operator-selected example, not a claim about your account's
quota; coordinate it with other users of the same provider. Live requests use
your provider account. `--prepare-only` records work without reading API_KEY or
sending requests. A missing key during processing produces a visible missing-api-key
error; set the environment credential and explicitly retry that document.

| Environment variable | Use |
|---|---|
| `API_KEY` | Live bearer credential; runtime only, never saved in settings/evidence |
| `MODEL_ID` | Intake/evidence model default; pinned snapshot above if unset |
| `MAX_OUTPUT_TOKENS` | Intake/evidence output-token cap; default 4096 |
| `REASONING_EFFORT` | Intake/evidence reasoning effort; default none |

CLI `--model`, `--max-output-tokens` and `--reasoning-effort` override those defaults
for OpenAI intake/evidence. Effort is one of none/low/medium/high/xhigh; model
alternatives must support this request configuration and strict structured output.
Use a pinned snapshot for repeatable configuration. Model access/availability and
parameter compatibility still depend on the provider/account; invalid combinations
are permanent errors, not silently rewritten. New intake settings refresh affected
results; resume/retry keep saved settings regardless of changed environment defaults.
For provider changes use fresh intake. Mock intake requires its fixture map;
OpenAI intake rejects a fixture map and OpenAI retries reject replacement fixtures.

The request keeps trusted instructions separate from the message, constrains
`text.format` to the supplied schema, and still validates returned facts/evidence
locally. The adapter reads only assistant `output_text` content, skips reasoning
items, rejects tool/ambiguous output, and detects refusal even when text is present.
Non-completed responses never yield usable extraction text. Successful provenance
uses the actual response model ID (`openai/<model>`), including when an alias was
requested. [Structured output handling](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)

HTTP 408/409/429/500/502/503/504 and retryable failed responses use the durable
scheduler's budget; credentials, permission, invalid-request/model and exhausted
quota errors are permanent. Retry-After is honored. Responses are bounded to 2 MiB;
malformed envelopes and raw error bodies are discarded with safe diagnostic codes.
[Provider error guidance](https://developers.openai.com/api/docs/guides/error-codes)

Generate credential-redacted request evidence without a key or network call:

```bash
env -u API_KEY python3 -m it_access request-evidence materials/requests/IT-001.json --out deliverables/request-evidence.json
```

This example uses the same request builder and canonical instruction formatter as
intake and includes model/schema/generation settings, transport controls, worker
options, source hash and implemented response handling. It is labeled
`request-example-not-sent`; it does not claim provider acceptance or live accuracy.
The API payload/configuration is documented in the
[Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create).

Step 3 evidence is in `planning/step-03-red.txt`, `planning/step-03-green.txt`, and
`planning/step-03.md`. Step 4 evidence is in `planning/step-04.md`, its red/green test
logs and its recorded mock queue. Independent expected routes and their source
quotes are in `tests/expected/routing.json`. Step 5 tests, observed repairs and
mock recovery outcomes are documented in `planning/step-05.md` and its supporting
logs/observations. Step 6 transport tests, API references, observed evidence repair
and limitations are recorded in `planning/step-06.md`. All observed model/transport
tests so far are mocked; no live accuracy claim is made. Consistent extraction/queue
deliverable exports and independent field-accuracy checks remain step 7.
Do not commit credentials or environments.
