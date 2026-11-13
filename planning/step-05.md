# Step 5: scheduling and recovery

Implements T21, T22, T24, T28 and T29 with bounded workers, durable call budgets,
rate coordination, renewable claims, graceful interruption and recovery commands.

## Changes

- WorkerOptions validates concurrency (1–32), call budget (1–10), transport timeout,
  lease length, exponential backoff cap and optional requests-per-minute spacing.
  Four worker threads are the default; each holds at most one claim. Each result
  is committed independently. Model work still holds no database transaction.
- Providers accept timeout_seconds and report retryable errors with optional
  Retry-After. Timeouts/connections and explicitly transient provider errors retry;
  refusal, incomplete/malformed/invalid extraction and permanent errors do not.
  Unknown exceptions at the provider boundary become sanitized terminal errors.
- Retry delays use bounded exponential backoff and jitter. Retry-After seconds
  and HTTP dates are supported, and shared provider/model cooldowns are retained
  in SQLite. Hints beyond the automatic wait budget fail visibly instead of
  retrying too early. Request admission is transactional across operator processes;
  configure a consistent quota for all operators sharing the database.
- Heartbeats renew still-live claims during calls and waits. Recovery requeues
  only expired current claims and issues fresh tokens; old tokens remain fenced.
  Pending work survives process exit, finalized work is reused, and stale workers
  cannot overwrite a newer attempt.
- `resume` recovers expired work once on startup and processes available jobs.
  `retry` creates a fresh current attempt using saved bytes/settings; its finalized
  predecessor remains unchanged. Overlapping retries serialize, and unfinished
  jobs must use resume. An optional replacement mock fixture supports reproducing
  failures. Retry processes its target document only.
- History includes append-only processing events: acceptance, claim, worker options,
  provider-call reservation, sanitized failure/backoff, recovery, release, explicit
  retry linkage and final outcome. Claims/call counts are inspectable; raw provider
  exceptions and claim tokens are not exposed. Existing databases gain the tables
  without rewriting attempts or fabricating older events.
- Provider-call reservations count toward the same durable attempt's budget across
  interruption/recovery. Explicit retry creates a new budget. Operational worker
  options are recorded in events; changing worker concurrency does not invalidate
  an already-completed extraction or alter its source/model/policy fingerprint.
- CLI SIGINT/SIGTERM handlers stop claims cooperatively, interrupt waits and release
  unfinished owned work once the provider returns. Summary includes unfinished work
  and interruption state; interrupted exits are 130/143. Queue exposes lease expiry
  and uses interrupted/resume-processing for an expired claim.

## Test-first evidence and observed repairs

The initial full-suite run failed because WorkerOptions did not exist; all 67
existing tests passed. The initial tests and `planning/step-05-red.txt` were
committed as `b5df1c4` before implementation.

The first implementation run had 87 tests with one failure: a concurrency test
assumed that short sleeps would produce overlapping calls. SQLite scheduling
allowed the calls to finish sequentially. The test was corrected to synchronize
two callers with a barrier instead of relying on timing. The Retry-After test's
fake wait was also made to advance its fake clock. An explicit exponential
backoff/connection-recovery check was added (already passed). All 88 then passed.

Further checks covered real SIGTERM behavior, overlapping explicit retries and
preserving the call budget across resume. The first two already passed; the new
budget check failed: resumed work used two more calls instead of the one remaining.
Tests and the full failing output `planning/step-05-budget-red.txt` were committed
as `177b33d` before the repair. Call numbering/remaining budget now derive from
persisted reservations rather than restarting at one for every claim.

Final command, Python 3.12.3, no API key:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

Observed: **91 tests passed**; full output is `planning/step-05-green.txt`.
New assertions cover:

- T21: visible null/error extraction, batch isolation, safe error messages,
  transient recovery, timeout argument propagation and terminal budget exhaustion.
- T22: a completed result can be inspected while another provider call is blocked;
  synchronized callers demonstrate concurrency bounded by the configured workers.
- T24: live lease renewal; expired-only recovery; token fencing; a heartbeat during
  a slow call; abrupt subprocess exit followed by CLI resume without redoing prior
  completed work; graceful stop; an actual rate-waiting CLI subprocess terminated
  with SIGTERM and returning exit 143 with remaining work pending; persisted budget
  across interruption/resume.
- T28: new attempt after prior success, failed retry current with prior success in
  history, reproducible CLI refusal, unfinished/unknown retry errors, and two
  overlapping retry callers producing only one pending attempt.
- T29: refusal/incomplete/malformed/schema failures get one call; bounded backoff,
  seconds/date Retry-After, long-hint early-retry prevention, database-shared rate
  admission/cooldown, connection failure recovery, and invalid configuration bounds.

## CLI observations

`planning/step-05-observations.json` records actual CLI calls in an isolated
temporary database, all response_source=mock, model label fixture, pipeline 4.0,
routing version 1, prompt v1 and the supported policy from step 4. Default worker
options are saved alongside the results. These were observations, not live calls:

1. Intake IT-001 using its saved success fixture: one completed review result.
2. Explicit retry with refusal fixture: new current error/null extraction; the first
   review result remains historical (attempt IDs 1 and 2).
3. Explicit retry with success fixture: new completed current review result.
4. Prepare another independent document, claim it in a child process with a 0.1s
   lease, and exit abruptly. After expiry, CLI resume reports recovered=1,
   processed=1, unfinished=0; previously completed work is reused.

README documents intake, queue, resume, retry, history and every worker flag.
`uv lock --check --offline` and `git diff --check` passed. No external dependency
was added, no live model was called, and existing Entire settings were untouched.

## Operating limits

Supported operation remains multiple processes sharing a local SQLite database on
one machine. Per-process concurrency is bounded, while the optional shared rate
gate coordinates only this database's provider/model scope; it cannot account for
unrelated tools using the same external quota. Rate spacing defaults to disabled
(0) for reproducible local mocks; configure it for a live quota later.

The timeout is a transport contract passed to adapters and tested with simulated
timeouts. No live transport exists until step 6. Python threads cannot forcibly
cancel arbitrary adapters that ignore the timeout. Graceful shutdown waits for
the in-flight bounded transport operation to return; hard process termination
relies on lease recovery. Leases should comfortably exceed database lock/scheduler
delays. A resume invocation does not wait for still-live foreign claims to expire;
inspect unfinished work and invoke it again after expiration when appropriate.

Retry-After cooldowns may keep other jobs waiting beyond the per-job retry budget;
those waits remain cancellable and the queue remains readable. Call reservations
are conservative: a crash before sending may consume a budget slot, and a crash
after sending can cause a repeated external request on recovery. Exactly-once
external calls are not guaranteed. Provider integrations must avoid hidden SDK
retries that would bypass these recorded call counts.

All accuracy observations remain mock-only. Local concurrency/timing tests and a
short child-process failure do not establish production throughput, network-outage
behavior of an actual provider, or the 2,000-document workload target. Live request
handling is step 6; full load checks, consistent deliverable exports and independent
field/factual accuracy validation remain step 7.
