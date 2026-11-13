# IT-003 review follow-up

These operator-prepared source exports preserve the user-provided follow-up:
start 2026-11-01, end 2027-01-31, staging/read, staging/write and production/read.
The schema allows one environment/permission per document, so three linked
documents retain request_id IT-003. Original supplied messages and frozen
expectations remain unchanged. No live access was granted.

The matching fixtures are explicitly authored mocks, not live provider replies.
Reproduce this supplementary review dataset separately from the 20 originals:

```bash
env -u API_KEY python3 -m it_access intake materials/review-updates --provider mock --fixture-map tests/mock_responses/review-updates/fixture-map.json --db var/review-scopes.db
python3 -m it_access export --db var/review-scopes.db --out deliverables/review-updates
python3 -m it_access verify-export --out deliverables/review-updates --sources materials/review-updates
```

Each result remains in review. System Owner reviews staging/read. Security Review
reviews staging/write and production/read, then forwards to the system owner.
