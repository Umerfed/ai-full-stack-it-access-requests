# Step 1: mock foundation

Implemented T01 from planning/plan.md: an explicit disk-fixture mock provider,
provider response contract, CLI replay, Python project metadata and uv lockfile.
The fixture is manually authored from IT-001 and is not an accuracy oracle.

## Observed verification

- `env -u API_KEY python3 -m unittest discover -s tests -v` initially failed
  because `it_access` did not exist; three starter tests passed. The retained
  output is `planning/step-01-red.txt`.
- The same command after implementation passed all nine tests. The retained
  output is `planning/step-01-green.txt`. New tests cover offline replay,
  deterministic behavior, unreadable/invalid fixture errors, CLI replay and
  CLI failure without traceback.
- `uv lock --offline` resolved the project with no external dependencies.
- `env -u API_KEY python3 -m it_access --help` and
  `env -u API_KEY python3 -m it_access mock-reply --fixture tests/mock_responses/complete-001.json`
  both exited successfully; replay returned a mock-labeled response envelope.

No live provider call, extraction-schema validation, routing, database intake or
accuracy measurement is claimed by this step. Fixture-envelope checks only
establish the provider contract. Completion/refusal handling belongs to step 2.

The failing test was run before implementation, but these changes have not been
committed. The saved logs are working-tree evidence, not the separate test-first
Git commits described in the course guide. Do not claim that commit ordering was
established retroactively. No changes were made to the existing capture settings.
