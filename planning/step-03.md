# Step 3: durable intake and revisions

Implements T04, T13, T23, T25–T27 from the plan. Source identification, configuration
versioning, local shared operation and history are implemented; routing is step 4,
and lease recovery/retry/concurrent worker scheduling is step 5.

## Design and operator behavior

`it_access.storage.Store` uses SQLite on local disk with WAL, foreign keys,
FULL synchronous writes, per-operation connections, and bounded lock waits.
Writes use BEGIN IMMEDIATE; model work occurs outside transactions. Current
records and history each come from a single SELECT, so their individual reads
are consistent. Each accepted document revision gets a monotonically increasing
attempt ID within the shared database. Filename identifies a document; original
bytes determine its source SHA-256. Canonical JSON of processing settings determines
the settings SHA-256. Input filenames must be unique within one CLI batch.

An unchanged current source/settings pair reuses its attempt, regardless of
processing state; it does not silently retry a failure. Changed inputs/settings
create a new attempt, and omitted documents remain current. If input returns to
an earlier historical version, it creates another attempt; historical records
are never made current again. This preserves acceptance order and follow-up.

Each attempt retains source bytes, prompt, policy, schema, provider/model,
generation settings, pipeline version and mock reply snapshot. The mock adapter
can replay that saved reply without reopening the fixture. Source/configuration
files changed after preparation cannot change what a worker processes. Source
content remains untrusted data and is passed separately from instructions.

Claims are transactional and token-protected. Only the current pending attempt
can be claimed. Successful/failed completion requires a live token and lease,
and that attempt must still be current. Accepting a newer revision supersedes
unfinished old attempts and revokes their claims; completed/error attempts remain
unchanged. Current failures have null extraction and their own error; history
retains prior successes. Worker errors are not a route decision yet.

The CLI exposes `intake`, `records` and `history`. `intake --prepare-only` separates
durable preparation from processing; rerunning intake processes pending work.
The current fixture map supports ten supplied documents. README contains exact
commands and constraints; the twenty-document example awaits remaining fixtures.

## Observed verification

Before implementation, the full suite failed to import `it_access.intake`; all
26 previous tests passed. The retained log is `planning/step-03-red.txt`, committed
with tests in `e083b57` before implementation.

After implementation, 44 tests passed. Two additional checks (different-document
process concurrency and source/fixture changes after preparation) already passed
when added; they are recorded honestly in commit `32b7838` with “already passes”.

The final full-suite run passed 46 tests on Python 3.12.3, without API_KEY:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

Output: `planning/step-03-green.txt`. Assertions include:

- T04: exact filename and independent original-byte hash; CLI intake, current
  records, history and prepare-only behavior.
- T13: two filenames stating the same business request ID retain independent
  current records (filename mismatch routing is still step 4).
- T23: separate processes initialize/share a fresh database and process the same
  document once; different-document processes preserve both results; repeated,
  wrong-token, expired-token and superseded completions cannot publish.
- T23: a newer failed attempt remains current while prior success stays in history.
- T25: unchanged completed intake makes no extra provider calls; unchanged pending
  jobs survive reopening without duplicate attempts.
- T26: changed raw bytes and each relevant setting create revisions; current
  pointers follow acceptance order; history keeps earlier hashes/extractions.
  Configuration key order does not cause a refresh; settings snapshots do not
  change when the caller mutates its dictionary. Prepared source/fixture snapshots
  survive later file edits and do not reload current extraction configuration.
- T27: subset batches retain documents omitted from later intake.

The README CLI example was also run with an isolated temporary database:
first intake accepted two documents with attempt IDs 1/2 and processed two jobs;
an unchanged rerun returned IDs 1/2 and processed zero. `records` returned two
completed current records; `history IT-001.json` returned its single completed
attempt. `uv lock --check --offline` and `git diff --check` also passed.

## Limits and remaining work

Observations are mock-based and on a local filesystem; the process tests use
Linux fork and two operators, not a scale/load test or multi-host deployment.
The worker is sequential and uses fixed 60-second claims without renewal/recovery.
Expired running work remains visible until step 5 implements safe recovery.
Claims protect publication, not exactly-once external provider execution across
crashes; a future recovered job may need another model call.

Policy routing, approvals, queue exports, provider transport timeouts/backoff,
explicit retry and the full 2,000-document operating check are not established by
this step. Processing-version constants must be bumped for code changes that alter
output semantics. Future schema vocabulary changes require validator changes as
noted in step 2. Credentials must never be placed in processing settings or fixtures.
No changes were made to the pre-existing Entire configuration.
