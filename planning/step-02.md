# Step 2: extraction contract

Implements the planned T02–T03 and T05–T11 checks. T04 remains assigned to
step 3. Routing, storage, retries and live provider integration remain later steps.

## Changes

- Versioned extraction prompt in `prompts/extraction-v1.txt`, combined with the
  original office policy and schema as trusted provider instructions.
- Provider contract accepts instructions separately from message text; export
  timestamps, subjects and other metadata are not sent as business evidence.
- `it_access.extraction.extract` validates input and calls the configured provider.
  `validate_response` requires completion and no refusal before parsing JSON.
- Strict JSON parsing rejects duplicate keys and nonfinite constants. Validation
  checks required fields, types, enums, additional fields, array element types,
  calendar-valid dates and literal supporting quotes against the decoded message.
- Every null field needs an explanatory missing/relative/conflict issue; such an
  issue cannot coexist with a resolved value for the same field. Stable error
  codes distinguish response, JSON, schema, date, evidence and issue failures.
- New source-authored mock fixtures cover missing facts, conflicts, corrections,
  relative dates, requested permission changes, refusal, interruption and bad output.

## Observed verification

The first full-suite run failed because `it_access.extraction` did not exist;
the nine existing tests still passed. Its output is `planning/step-02-red.txt`.
The tests, fixtures and red output were committed as `b4bf160` before the
implementation was added.

After implementation, this command passed all 26 tests on Python 3.12.3:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

Output is retained in `planning/step-02-green.txt`. The offline harness removes
API_KEY and rejects socket construction/connections. Tests include:

- T02: IT-001 source facts, omission and wrong type for every required field,
  additional fields, invalid enums and invalid array elements.
- T03: message-only model input, separate instructions and invalid input handling.
- T05: refusal (including with otherwise valid output), incomplete responses,
  malformed JSON, duplicate keys, nonfinite constants, schema and calendar errors.
- T06: fabricated, empty and absent quotes rejected.
- T07: nulls and field-specific issues for IT-003/IT-010; unexplained nulls rejected.
- T08: IT-004 environment and IT-020 manager conflicts preserve alternatives and
  unresolved values; a conflict issue with a resolved value is rejected.
- T09: IT-008 corrected staging environment and IT-015 corrected dates retained.
- T10: IT-011 relative dates remain null despite export metadata.
- T11: IT-007 requests write and IT-019 requests read, both as changes.

## Limits

All observations use mocks; none measures live model accuracy. Fixtures encode
the source-grounded interpretations used to test the validation pipeline. A
model could still omit a conflict, choose an unsupported scalar, omit an override,
or return incomplete but literal evidence. Semantic interpretation and complete
evidence coverage require the later independent accuracy/factual review checks.
The validator supports the vocabulary in the supplied schema; extending that
vocabulary requires extending validation and its tests. Reversed date ranges
are deliberately preserved for policy clarification in step 4. Errors are raised
without partial extractions; durable error records and retry policy come later.
