# Verification and operating limits

All model observations in this submission are **mock** examples. The OpenAI adapter is implemented and its HTTP behavior is tested with fabricated envelopes; no live provider request or accuracy observation was performed. Machine-readable records carry response_source=mock. The OpenAI request evidence is separately labeled request-example-not-sent.

## Commands, assertions and observed results

Run from the repository root with Python 3.12+ on Linux/POSIX, SQLite JSON functions and symlink support. There are no external runtime dependencies. These setup and full-suite commands were executed in a fresh locked virtual environment:

```bash
uv sync --locked --offline
env -u API_KEY .venv/bin/python -m unittest discover -s tests -v
python3 tools/check_prerequisites.py
```

Observed: 132 tests passed in the fresh environment. Output is [planning/test-run.txt](../planning/test-run.txt); the step-specific log is [planning/step-07-green.txt](../planning/step-07-green.txt). Prerequisites report READY, 4/4 checks, including 132 tests without API_KEY; the exact output is [planning/prerequisites.txt](../planning/prerequisites.txt). uv resolved the one virtual project with no third-party packages. Python was 3.12.3, SQLite 3.45.1. Tests disable credentials and unexpected network access; provider-boundary tests use only sentinel credentials and fabricated responses.

| Check | Intended comparison | Observed result |
|---|---|---|
| Extraction contract | Supplied schema; completion/refusal; dates; null issues; quotes against decoded source | Complete, missing/conflicting/corrected/relative/change cases and invalid responses covered |
| Human routing | Five rules in policy order against source-derived expected tuples | 20/20 original routes; no automatic approvals |
| Versioning | Filename/original-byte hash, processing fingerprint, current pointer and old history | Unchanged reuse, changed source/settings, subset batches, duplicate business IDs and failed newer attempts covered |
| Shared operation | Overlapping processes/claims, stale/expired tokens and persisted state | Same/different document process cases and overlapping manual retries covered |
| Interruption | Ready output during slow work, real subprocess exit/SIGTERM, renewable leases and durable call budgets | Pending work recovered; completed work reused; resumed calls respect recorded budget |
| Provider handling | Mocked exact wire request/response shape, retries, deadlines, rate limits and secrets | TLS, connected-I/O watchdog, no redirect/hidden retry, safe errors/history/evidence covered |
| Export integrity | Same read/attempt/source/config/routing/snapshot; complete publication and file hashes | Previous pair survives failed publication; pending export refused; overlapping exporters and tamper detection covered |
| Accuracy procedure | Frozen source-derived fields, fixed denominators and separate evidence/issue/routing checks | Wrong facts detected, failures/nulls/missing/duplicate records tested; 21 expected messages verified against their sources |

Reproduce the 20 original outputs and their independent comparison:

```bash
env -u API_KEY python3 -m it_access intake materials/requests --provider mock --fixture-map tests/mock_responses/fixture-map.json --db var/submission.db
python3 -m it_access export --db var/submission.db --out deliverables
python3 -m it_access verify-export --out deliverables --sources materials/requests
python3 -m it_access accuracy --expected tests/expected --records deliverables/extractions.jsonl --out deliverables/accuracy-report.json
```

Observed: 20 accepted/processed, zero unfinished; 20 unique current records and linked queue entries. Every original byte hash, required field/type, successful error=null, mock response source and queue/extraction reference passed verification. The published pair has common snapshot ID 7387cdbe8bb863b1fe6d0a236edba3258098adaa008293cd797a78b28e2fb26d. Accuracy is **237/240 exact scalars**, **20/20 routes**, **23/23 literal quotes**, **227/227 fixed evidence spans**, **24/24 required issue flags**, with no source-version or record-validation errors. Three source-grounded purpose-wording differences remain counted as exact failures; the agent source review observed zero factual errors. Full expected values, counting rules and actual discrepancies are in [accuracy.md](accuracy.md) and the JSON reports. This is not model performance or human sign-off.

The independent paraphrase is a separate dataset:

```bash
env -u API_KEY python3 -m it_access intake tests/inputs/paraphrase.json --provider mock --fixture-map tests/mock_responses/paraphrase-map.json --db var/paraphrase.db
python3 -m it_access export --db var/paraphrase.db --out deliverables/paraphrase
python3 -m it_access verify-export --out deliverables/paraphrase --sources tests/inputs
python3 -m it_access accuracy --expected tests/expected --records deliverables/paraphrase/extractions.jsonl --group paraphrase --out deliverables/paraphrase-report.json
```

Observed: 12/12 exact scalar fields, 1/1 route and valid source quotes; request_date remains the stated 2026-10-22 despite export metadata dated 2099. Its snapshot ID is 75c104f8a490d9f749d372e72b7f82a17d2102e5dbd23e1b446714631e089f39. The root extractions file remains exactly the 20 supplied originals.

Request evidence can be regenerated independently without credentials or network:

```bash
env -u API_KEY python3 -m it_access request-evidence materials/requests/IT-001.json --out deliverables/request-evidence.json
uv lock --check --offline
git diff --check
```

Observed: the redacted body matches the actual intake builder and instruction formatter, includes the configured pinned model/schema/parameters and handling rules, and contains a credential placeholder only. Lockfile and whitespace checks passed. The not-sent OpenAI request configuration is described in [planning/step-06.md](../planning/step-06.md); it did not generate the mock extraction records.

## Workload and restart observation

```bash
env -u API_KEY python3 tools/verify_workload.py --documents 2000 --workers 4 --out deliverables/workload.json
```

The command was executed once with the 20 supplied source/fixture pairs repeated under independent filenames retaining each IT identifier, four configured workers, rate spacing disabled, zero backoff and declared synthetic latency of 1 ms. Extraction configuration was mock/fixture, generation {}, prompt v1, pipeline 4.0 and routing version 1, with the same supplied policy/schema and 20 fixture pairs as the original comparison. One call was deliberately held while completed records were inspected. The worker was stopped cooperatively, another process claimed unfinished work and exited abruptly, and processing resumed after its short lease expired. These are explicit simulated faults, not observed provider outages.

Observed total duration **80.56s**: preparation 13.98s; first phase 2.58s; recovery/resume 60.91s; unchanged inspection 2.19s; export/verification 0.90s. Peak RSS **138184 KiB (~134.9 MiB)** on Linux-6.12.114+-x86_64-with-glibc2.39, 8 reported CPUs.

**48** records were complete before stopping and none was reprocessed. One expired claim was recovered; 1952 jobs were claimed after resuming. All 2,000 documents completed with source-derived routes, unique current identities, correct source hashes and no error records. 2001 mock calls include the repeat of abandoned in-flight work; exactly-once external calls are not claimed. Observed peak provider concurrency was 2, within the configured bound of 4. The unchanged rerun processed zero jobs. Snapshot verification, SQLite integrity_check and foreign_key_check passed; attempt count stayed 2,000. Raw configuration, timings, assertions and machine context are in [workload.json](workload.json).

This checks local fixture processing, incremental readiness, interruption recovery and storage coherence. It does not establish live model latency/accuracy, quota capacity, multi-host support or production throughput. Two simultaneous operator processes are covered separately by unit tests; the workload uses one bounded scheduler plus the abruptly exited claiming process.

## Observed repairs

| Stage | Observed failure | Repair/evidence |
|---|---|---|
| Step 5 concurrency test | A short sleep did not reliably overlap callers | Replaced the timing assumption with a synchronization barrier; step-05.md records it |
| Step 5 resumed budget | Interruption reset provider-call numbering and allowed too many calls | Count persisted reservations across claims; step-05-budget-red.txt and passing recovery test |
| Step 6 request evidence | Example and actual request instruction strings differed in schema formatting/order | Shared canonical instruction formatter; step-06-evidence-red.txt and exact wire-body equality test |
| Step 7 build | Accuracy module did not yet exist in the initial test-first run | Source-derived ledger/tests committed as 08a6fe6 before code; step-07-red.txt and passing logs |
| Step 7 comparison | Three purpose strings differed from the preselected expected spans | No expectation/output rewrite; retained 237/240 and recorded source review separately |

## Operating assumptions and limits

The application supports regional operators sharing one local SQLite database on a single Linux/POSIX machine. WAL readers see ready results while other requests run; short transactional writes, claim tokens and renewable leases protect coherent current pointers. No network-filesystem/multi-host database support is claimed. Current source/config snapshots are recorded per attempt. Omitted batch documents remain present. Changed source or output-affecting settings create a revision; old successes remain history when a newer attempt fails. Unknown policy text requires reviewed routing changes and refresh, not automatic interpretation of a new policy.

Providers and worker limits are configurable as documented in README. Shared rate coordination covers this database/provider/model scope, not unrelated internal tools using the same provider quota; operators should use consistent limits. Retry-After cooldowns can postpone other jobs and remain interruptible. Provider calls are reserved before invocation and count toward the durable attempt's budget; a crash before sending can consume a reservation, while a crash after sending can require another call on recovery. SDK-free transport has no hidden HTTP retry. Current timeouts cover sockets and connected-I/O shutdown; OS DNS resolution may exceed that deadline. Python cannot safely preempt arbitrary custom adapters ignoring their timeout. Ctrl+C/SIGTERM release cooperative unfinished claims after the in-flight call returns; abrupt exits require lease expiry. Resume checks expired claims once at startup and does not steal live work. Errors require explicit retry, which creates a new attempt and keeps history.

Exports require finalized/routed current work and capture one read under a local export lock. Immutable directories hold a complete candidate before the common pointer switches. Readers must pin .current once; independently opening the public files during publication can cross snapshots and must be detected through IDs. The verifier pins the directory and compares hashes and references. Old snapshot directories must remain available to ongoing readers. History/snapshot retention and backups are operator responsibilities. Export crash/power-loss guarantees depend on the local filesystem; tests inject publication failure, not hardware faults.

Extraction validates the supplied schema vocabulary, completion status, dates, literal quotes and null issues. The prompt/model interpret business language; no general factual correctness or injection immunity is claimed. Fixed source-derived expectations and source review are therefore separate from structural checks. Free-text issue explanations and arbitrary fresh messages still need human assessment. Tests and benchmarks use mocks. Live account/network/model access and model accuracy remain unmeasured, even though the provider integration is implemented.

## Requirement audit and submission boundary

[planning/validation.md](../planning/validation.md) has one row for each of the 54 spec requirements: **52 proposed met, 2 not met**. These are agent evidence recommendations for the user to inspect, not the human sign-off requested by the course guide. Capture is enabled according to the prerequisite checker, but the final commit/session association has not been verified. The assigned submission repository/default branch, final push and Classroom root-link submission remain step 8. Do not interpret READY or passing unit tests as proof of those submission actions.
