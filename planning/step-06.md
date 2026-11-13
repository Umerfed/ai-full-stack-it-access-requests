# Step 6: live provider integration and request evidence

Implements T30/T31: configurable OpenAI Responses REST integration, credential
redaction, explicit response/error handling and a reproducible request example.

## Configuration and verified contract

The supported default request selects `gpt-5.4-mini-2026-03-17`, reasoning effort
none and 4096 output tokens. Official documentation lists this snapshot, Responses
and structured-output support, and none/low/medium/high/xhigh effort. Older model
options inspected during research were deprecated, so they were not selected.
[Model reference](https://developers.openai.com/api/docs/models/gpt-5.4-mini)

POST `/v1/responses` carries instructions separately from a user message, strict
`text.format` JSON Schema, model/generation parameters, store=false and stream=false.
The credential is a runtime bearer header. Requests have no tools/conversation state.
[Responses create reference](https://developers.openai.com/api/reference/resources/responses/methods/create)

Completed responses require a completed assistant message; refusal/incomplete
responses cannot supply usable facts. This follows the structured-output guide's
requirement to handle those cases outside the schema constraint.
[Structured output guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)

Known temporary HTTP errors and Retry-After enter the existing scheduler's bounded
retry path. Authentication, permissions, invalid model/request and quota/spend
exhaustion become actionable permanent errors, with raw error messages discarded.
[Error-code guidance](https://developers.openai.com/api/docs/guides/error-codes)

These API/model facts were checked during this step (planning date 2026-11-13).
They establish the request contract, not model access or a successful live request.

## Implementation

- `it_access/openai_provider.py`: pure request builder, standard-library HTTPS
  transport, response-envelope adapter, configuration validation and evidence
  builder. The transport verifies TLS, performs one POST, does not follow redirects,
  does not retry internally, caps response bodies at 2 MiB, applies socket timeouts
  and uses a post-connect socket shutdown watchdog to interrupt blocked I/O.
- The provider reads API_KEY only at runtime and stores it only on its private
  adapter instance. Configuration snapshots hold model/generation, schema,
  endpoint/request version, prompt/policy and existing pipeline/routing versions;
  credentials are excluded. Successful response_source uses the actual returned
  model ID rather than assuming it equals the requested alias/snapshot.
- Response parsing requires a valid Responses envelope, a known status and model
  ID. It skips reasoning nodes, accepts assistant output_text, rejects unexpected
  tools or multiple assistant messages, and detects refusal even alongside text.
  Non-completed responses expose no partial text. Existing local JSON/schema,
  date/evidence/issue validation and ordered human routing remain in use.
- Only known safe error codes/messages enter history/events. Provider wire bodies,
  raw exception text, credentials and claim tokens do not. HTTP status/known quota
  codes determine retry classification, with Retry-After passed to the scheduler.
- Intake supports explicit `--provider openai` with model/token/effort flags and
  MODEL_ID/MAX_OUTPUT_TOKENS/REASONING_EFFORT defaults; API_KEY is runtime-only.
  Defaults stay mock unless OpenAI is explicitly selected. Resume/retry use saved
  per-attempt settings; provider-filtered claims prevent mock-only runs from
  accidentally invoking pending OpenAI jobs. Changing provider requires intake.
- `request-evidence` creates `deliverables/request-evidence.json` without credentials
  or transport. The model request body comes from the real builder. Canonical
  schema/instruction formatting is shared with persisted intake so the example's
  body exactly matches the sent body under the same settings and message.
- Runtime is still Python 3.12+ with SQLite JSON functions and standard-library
  dependencies only. Existing pyproject.toml/uv.lock remain valid; no SDK with
  independent retry behavior was introduced.

## Test-first evidence and observed repair

The initial full-suite run failed because the new adapter module did not exist;
all 91 previous tests passed. Tests, fabricated REST fixtures and the red log
`planning/step-06-red.txt` were committed as `5b69e95` before implementation.

The first implementation passed 114 tests. Follow-up checks added a real timer
watchdog firing against a fake blocked connection, mock-provider isolation and
exact evidence/intake wire-body equality. The first two already passed. The
wire-body check failed because the evidence's schema formatting and the worker's
persisted/canonical schema formatting produced different instruction strings.
The new checks and full failing output `planning/step-06-evidence-red.txt` were
committed as `7369bba` before the repair. Both now use the same canonical formatter.

Final command, Python 3.12.3, no API key:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

Observed: **117 tests passed**, recorded in `planning/step-06-green.txt`. New checks
cover the exact request/schema/model/settings, timeout forwarding and actual socket
watchdog interruption, TLS verification, single POST/no redirects, body limits,
completed/refused/incomplete/failed responses, actual model provenance, malformed
or ambiguous output, transient statuses, permanent auth/request/model/quota errors,
runtime environment snapshots, scheduler-owned retry, missing credentials, safe
errors/history/output/evidence, evidence generation/equality and provider isolation.

Fixtures under `tests/mock_responses/openai/` are fabricated, credential-free API
envelopes marked by mock IDs; they are not captured model outputs. Tests replace
the HTTP transport or connection objects, keep real extraction/routing validation,
block unexpected socket access, and use only a sentinel credential. Temporary
integration records bearing openai/model identify the simulated adapter behavior;
they are not submitted as live-provider observations.

## Recorded operational checks

This command generated the committed example without any provider call:

```bash
env -u API_KEY python3 -m it_access request-evidence materials/requests/IT-001.json --out deliverables/request-evidence.json
```

A separate comparison confirmed its body equals build_request using the same
snapshot instruction formatter, its model ID is the documented pinned default,
and its Authorization header is the literal Bearer [REDACTED] placeholder. It is
labeled request-example-not-sent and includes source document/hash, worker options
and implemented response handling. No real credential was needed or inspected.

`resume --provider mock --db var/intake.db` reported recovered=0, processed=0 and
unfinished=0, retaining the earlier 20 completed mock records. `uv lock --check
--offline` and `git diff --check` passed. The pre-existing Entire settings change
was not altered. No live model call or live accuracy run was performed.

## Limits and next work

The supported endpoint is the direct OpenAI HTTPS Responses endpoint. Proxy/Azure/
OpenRouter compatibility is not claimed. DNS resolution is controlled by the OS
and may exceed socket timeouts; connected I/O has a tested watchdog. Graceful
shutdown still depends on in-flight transport completion, with lease recovery as
the abrupt-stop fallback. Account/network/model access and live provider behavior
remain unmeasured because all verification used mocks.

Request examples document configured behavior, not provider acceptance. Other
model IDs must support this schema and generation parameter combination; settings
are validated locally and unsupported provider combinations fail visibly. Pinned
snapshots are preferred to aliases; local caching does not detect alias retargeting.
Changing generation settings/model through new intake changes the fingerprint;
changing environment defaults alone does not rewrite saved jobs or past results.

Independent field/factual accuracy comparisons, the paraphrase, consistent current
extraction/queue exports, workload checks and final submission validation remain
step 7. Existing committed queue observations remain mock-labeled. Coding capture
pairing, final default-branch push and Classroom submission remain step 8.
