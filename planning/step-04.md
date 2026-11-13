# Step 4: human review routing

Implements T12 and T14–T20: the five ordered office-policy rules, annotated
extractions, persisted routing and a CLI review queue for all 20 supplied messages.

## Changes

- `it_access.routing.prepare_review` validates the extraction before applying
  policy and leaves its input unchanged. It retains message request IDs, detects
  filename-ID mismatches, flags reversed dates, and preserves override evidence.
- Rule order is explicit: override inspection; dated/identified withdrawal;
  clarification; Security Review for production/write/admin; System Owner for
  complete staging/read. No route approves access. A withdrawal can qualify
  despite missing access facts or filename mismatch because rule 2 precedes rule 3.
- Conflict and override markers follow the prompt's issue conventions. A limited
  source-text heuristic catches common override wording even if the model omits
  its marker, including the supplied IT-006 attack. It preserves message text as
  evidence, never executes source instructions, and does not read credentials.
- Review decisions are stored in `review_routes`, keyed by attempt ID. They are
  committed atomically with the annotated extraction or null/error result, under
  the existing current-attempt/token/lease guards. History retains earlier routes.
- `queue` reads persisted decisions through the same current-attempt join used
  by extraction records. Items carry document/attempt IDs, source hash, settings
  fingerprint, routing, processing state, request ID, issues and error/source data.
- Remaining fixtures and the filename map cover all 20 supplied messages. The
  independently authored expected routes in `tests/expected/routing.json` include
  source quotes, policy excerpts/rule numbers and clarification reasons.
- Pipeline version is now 4.0 and routing version 1. The supported policy SHA-256
  is `b949596765aef45377d9f8d0792f9113109b1f811ef4f3be26919c162e1f631a`.
  Unknown policy text fails before the provider call; changing business policy
  requires corresponding reviewed code/tests, followed by refreshed intake.
- Opening a step 3 database adds the routing table without rewriting attempts.
  Legacy completed extractions with no saved decision display refresh-required;
  reintake creates new routed versions and retains the old results as history.

## Observed verification

Before implementation, the new test module failed because `it_access.routing`
did not exist; all 46 existing tests passed. Tests, source-derived routing
expectations, fixtures and red output were committed in `8f3c5ba` before code.

After implementation, this command passed 67 tests on Python 3.12.3:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

The full logs are `planning/step-04-red.txt` and `planning/step-04-green.txt`.
Assertions cover all five rules, all required missing facts, other/unknown kind,
conflicts, requested new permissions, corrections, reversed dates, identity
mismatch, override priority over withdrawal, withdrawal priority over mismatch,
no approval output, queue/extraction linkage, unchanged reuse, latest failure
versus historical success, legacy database upgrade and unsupported policy.

The following actual mock commands were run against `var/intake.db`:

```bash
env -u API_KEY python3 -m it_access intake materials/requests --fixture-map tests/mock_responses/fixture-map.json --db var/intake.db
env -u API_KEY python3 -m it_access queue --db var/intake.db > planning/step-04-queue.json
env -u API_KEY python3 -m it_access intake materials/requests --fixture-map tests/mock_responses/fixture-map.json --db var/intake.db
```

Observed: the first intake accepted/processed 20 documents, attempt IDs 1–20.
The unchanged second intake reused those IDs and processed zero jobs. Every queue
item had response_source=mock and error=null. A separate comparison of the saved
queue against source-derived expected routes and the current database records
confirmed 20/20 matching routing tuples and matching attempt/hash/fingerprint
references. The recorded queue is `planning/step-04-queue.json`.

| Next action | Observed mock records |
|---|---:|
| review-request (System Owner) | 4 |
| review-risk-then-owner (Security Review) | 5 |
| clarify (IT Service Desk) | 9 |
| confirm-withdrawal (IT Service Desk) | 1 |
| inspect-message (IT Service Desk) | 1 |

`uv lock --check --offline` and `git diff --check` passed. Runtime dependencies
are unchanged. The existing Entire settings modification was left untouched.

## Limits and next steps

All recorded observations use mock provider `mock`, model label `fixture`, the
saved per-document fixture contents, extraction prompt v1, pipeline 4.0 and
routing version 1 with the supported policy hash above. This demonstrates route
and pipeline behavior for the fixture facts; it is not live model extraction
accuracy, complete factual verification, or general prompt-injection protection.
The router cannot discover every semantic conflict omitted by a model.

Pending/running queue items use processing labels pending/processing and
await-extraction; these are not successful policy routes. Legacy unrouted records
use refresh-required/refresh-intake rather than receiving retrospective decisions.
The CLI/database queue is available now; atomic deliverable exports and independent
field-accuracy comparison are planned for step 7. Fixed-lease recovery, explicit
retry, backoff, provider timeouts and concurrent scheduling remain step 5. Human
reviewers still make all approval decisions.
