# Step 7: accuracy, artifacts and handover

Implemented T32–T40 with a source-derived expectation ledger for the 20 original
messages and authored paraphrase, exact comparison and factual-review reporting,
consistent snapshot exports, full operator commands and an evidence-linked audit.

The initial failing run is planning/step-07-red.txt: the accuracy module did not
exist yet, while all 117 prior tests passed. Expected values, source quotes/null
reasons, paraphrase input/fixture and tests were committed in 08a6fe6 before the
comparison/export implementation. No expected value was edited after measurement.

After implementation, 132 tests passed (planning/step-07-green.txt). A fresh
uv sync --locked --offline environment also passed 132 tests without API_KEY;
its full output is planning/test-run.txt. The independent prerequisite checker
reported READY, including another 132-test mock run (planning/prerequisites.txt).

Published original snapshot: 7387cdbe8bb863b1fe6d0a236edba3258098adaa008293cd797a78b28e2fb26d.
It contains 20 unique current extraction records and a matching queue, all mock.
Original-byte hashes, schema, dates, quotes and linked attempt/config/routing
references passed verify-export. The separate paraphrase snapshot/report contains
one document and never changes the original denominator or 20-record artifact.

Observed comparison: 237/240 original exact scalar matches, 20/20 routes,
23/23 literal quotes, 227/227 fixed evidence spans and 24/24 required issue flags;
the paraphrase scored 12/12. Three business-purpose wording discrepancies were
retained. Source inspection judged them equivalent source-grounded spans and
observed zero factual errors in the agent review. This is not human sign-off or
live model accuracy. Full expected facts, rules and findings are in
 deliverables/accuracy.md, accuracy-report.json and factual-review.json.

The explicit 2,000-document mock workload completed in 80.56s, using about
135 MiB peak RSS on this machine. Forty-eight completed records survived
interruption without repetition; one abruptly exited claim was recovered;
all 2,000 final routes/hashes/IDs, unchanged reuse and SQLite/export invariants
passed. The observed peak provider concurrency was 2 within the configured bound
of 4. The 2,001 calls include abandoned in-flight work repeated on recovery.
Full timings, configuration, assertions and machine context are in
 deliverables/workload.json. No provider throughput/availability claim is made.

The root output files share an atomically switched .current pointer to immutable
snapshots. Publication is serialized, and interrupted publication keeps the old
pair. Readers must pin that pointer once or detect snapshot-ID disagreement;
verify-export does so. This design requires local Linux/POSIX file locking and
symlinks. Histories/snapshots are retained, with no automatic retention deletion.

planning/validation.md proposes 52 met / 2 not met across all 54 requirements and
links specific tests or artifacts. It is an agent audit pending user inspection.
Final commit/capture pairing and assigned-repository push/Classroom submission
remain step 8. The pre-existing Entire configuration change was left untouched.
