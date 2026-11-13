"""Transactional intake state for processes sharing a SQLite file on local disk.

Each operation uses its own connection. No model call holds a transaction open.
Completed attempts are never changed; documents point at the latest accepted
attempt. Publishing requires the current attempt, its claim token and a live lease.
"""
import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from it_access.extraction import message_from_export
from it_access.routing import check_routing_settings, prepare_review


DDL = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    current_attempt_id INTEGER REFERENCES attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    source_sha256 TEXT NOT NULL,
    source_bytes BLOB NOT NULL,
    settings_fingerprint TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    processing_state TEXT NOT NULL CHECK (
        processing_state IN ('pending', 'running', 'completed', 'error', 'superseded')
    ),
    created_at REAL NOT NULL,
    completed_at REAL,
    claim_token TEXT,
    lease_until REAL,
    claim_count INTEGER NOT NULL DEFAULT 0,
    extraction_json TEXT,
    error_json TEXT,
    response_source TEXT
);
CREATE INDEX IF NOT EXISTS attempts_document ON attempts(document_id, attempt_id);
CREATE TABLE IF NOT EXISTS review_routes (
    attempt_id INTEGER PRIMARY KEY REFERENCES attempts(attempt_id),
    status TEXT NOT NULL CHECK (status IN ('review', 'needs-information', 'error')),
    owner TEXT NOT NULL,
    next_action TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processing_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id),
    event TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_attempt ON processing_events(attempt_id, event_id);
CREATE TABLE IF NOT EXISTS provider_rate_limits (
    scope TEXT PRIMARY KEY,
    next_allowed REAL NOT NULL
);
"""


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class Claim:
    attempt_id: int
    document_id: str
    token: str
    lease_until: float
    source_bytes: bytes
    settings: dict


class Store:
    def __init__(self, path: str | Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WAL setup can race before busy_timeout takes effect in some SQLite
        # versions. Bound initialization retries; ordinary writes use busy_timeout.
        for attempt in range(5):
            try:
                with self._connection() as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(DDL)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def ingest(self, document_id: str, source_bytes: bytes, settings: dict) -> int:
        """Accept one source version; identical current input/settings reuse its ID."""
        if not document_id or Path(document_id).name != document_id:
            raise ValueError("document_id must be an input filename, without a directory.")
        encoded_settings = canonical_json(settings)
        fingerprint = hashlib.sha256(encoded_settings.encode("utf-8")).hexdigest()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        with self._write() as connection:
            current = connection.execute(
                "SELECT a.* FROM documents d JOIN attempts a "
                "ON a.attempt_id=d.current_attempt_id WHERE d.document_id=?", (document_id,)
            ).fetchone()
            if current and current["source_sha256"] == source_hash and current["settings_fingerprint"] == fingerprint:
                return current["attempt_id"]
            connection.execute("INSERT OR IGNORE INTO documents(document_id) VALUES (?)", (document_id,))
            if current:
                superseded = connection.execute(
                    "UPDATE attempts SET processing_state='superseded', claim_token=NULL, lease_until=NULL "
                    "WHERE attempt_id=? AND processing_state IN ('pending','running')",
                    (current["attempt_id"],)
                )
                if superseded.rowcount:
                    self._event(connection, current["attempt_id"], "superseded", {})
            cursor = connection.execute(
                "INSERT INTO attempts(document_id,source_sha256,source_bytes,settings_fingerprint,"
                "settings_json,processing_state,created_at) VALUES (?,?,?,?,?,'pending',?)",
                (document_id, source_hash, source_bytes, fingerprint, encoded_settings, self.clock())
            )
            attempt_id = cursor.lastrowid
            connection.execute("UPDATE documents SET current_attempt_id=? WHERE document_id=?",
                               (attempt_id, document_id))
            self._event(connection, attempt_id, "accepted", {})
            return attempt_id

    def _event(self, connection, attempt_id: int, event: str, details: dict):
        connection.execute(
            "INSERT INTO processing_events(attempt_id,event,occurred_at,details_json) VALUES (?,?,?,?)",
            (attempt_id, event, self.clock(), canonical_json(details))
        )

    def record_event(self, claim: Claim, event: str, details: dict) -> bool:
        with self._write() as connection:
            owned = connection.execute(
                "SELECT 1 FROM attempts WHERE attempt_id=? AND processing_state='running' "
                "AND claim_token=? AND lease_until>?", (claim.attempt_id, claim.token, self.clock())
            ).fetchone()
            if not owned:
                return False
            self._event(connection, claim.attempt_id, event, details)
            return True

    def provider_call_count(self, claim: Claim) -> int:
        with self._connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM processing_events WHERE attempt_id=? AND event='provider-call'",
                (claim.attempt_id,)
            ).fetchone()[0]

    @staticmethod
    def _record(row) -> dict:
        record = dict(row)
        record.pop("source_bytes")
        record.pop("claim_token")
        record["settings"] = json.loads(record.pop("settings_json"))
        record["extraction"] = json.loads(record.pop("extraction_json") or "null")
        record["error"] = json.loads(record.pop("error_json") or "null")
        record["is_current"] = bool(record["is_current"])
        return record

    def current(self) -> list[dict]:
        """One consistent read of the latest accepted attempt per document."""
        with self._connection() as connection:
            return [self._record(row) for row in connection.execute(
                "SELECT a.*, r.status, r.owner, r.next_action, 1 AS is_current "
                "FROM documents d JOIN attempts a ON a.attempt_id=d.current_attempt_id "
                "LEFT JOIN review_routes r ON r.attempt_id=a.attempt_id ORDER BY d.document_id"
            )]

    def history(self, document_id: str) -> list[dict]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            records = [self._record(row) for row in connection.execute(
                "SELECT a.*, r.status, r.owner, r.next_action, (a.attempt_id=d.current_attempt_id) AS is_current "
                "FROM attempts a JOIN documents d ON a.document_id=d.document_id "
                "LEFT JOIN review_routes r ON r.attempt_id=a.attempt_id "
                "WHERE a.document_id=? ORDER BY a.attempt_id", (document_id,)
            )]
            for record in records:
                record["events"] = [
                    {"event_id": row["event_id"], "event": row["event"], "occurred_at": row["occurred_at"],
                     "details": json.loads(row["details_json"])}
                    for row in connection.execute(
                        "SELECT * FROM processing_events WHERE attempt_id=? ORDER BY event_id",
                        (record["attempt_id"],)
                    )
                ]
            return records

    def claim(self, *, lease_seconds: float = 60, document_id: str | None = None, provider_name: str | None = None) -> Claim | None:
        """Exclusively claim current pending work with a fresh fencing token."""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")
        with self._write() as connection:
            row = connection.execute(
                "SELECT a.* FROM documents d JOIN attempts a ON a.attempt_id=d.current_attempt_id "
                "WHERE a.processing_state='pending' AND (? IS NULL OR a.document_id=?) "
                "AND (? IS NULL OR json_extract(a.settings_json,'$.provider')=?) "
                "ORDER BY a.attempt_id LIMIT 1", (document_id, document_id, provider_name, provider_name)
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            lease_until = self.clock() + lease_seconds
            connection.execute(
                "UPDATE attempts SET processing_state='running',claim_token=?,lease_until=?,"
                "claim_count=claim_count+1 WHERE attempt_id=?", (token, lease_until, row["attempt_id"])
            )
            self._event(connection, row["attempt_id"], "claimed", {"lease_seconds": lease_seconds})
            return Claim(row["attempt_id"], row["document_id"], token, lease_until,
                         row["source_bytes"], json.loads(row["settings_json"]))

    def _finish(self, claim: Claim, state: str, extraction, error, response_source: str, route: dict) -> bool:
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE attempts SET processing_state=?, extraction_json=?, error_json=?,"
                "response_source=?,completed_at=?,claim_token=NULL,lease_until=NULL "
                "WHERE attempt_id=? AND processing_state='running' AND claim_token=? "
                "AND lease_until>? AND attempt_id=(SELECT current_attempt_id FROM documents WHERE document_id=?)",
                (state, canonical_json(extraction) if extraction is not None else None,
                 canonical_json(error) if error is not None else None, response_source, self.clock(),
                 claim.attempt_id, claim.token, self.clock(), claim.document_id)
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO review_routes(attempt_id,status,owner,next_action) VALUES (?,?,?,?)",
                (claim.attempt_id, route["status"], route["owner"], route["next_action"])
            )
            self._event(connection, claim.attempt_id, state,
                        {"code": error["code"]} if error else {})
            return True

    def renew(self, claim: Claim, *, lease_seconds: float = 60) -> bool:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive.")
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE attempts SET lease_until=? WHERE attempt_id=? AND processing_state='running' "
                "AND claim_token=? AND lease_until>?",
                (self.clock() + lease_seconds, claim.attempt_id, claim.token, self.clock())
            )
            return cursor.rowcount == 1

    def release(self, claim: Claim) -> bool:
        """Return live owned work to pending on cooperative interruption."""
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE attempts SET processing_state='pending',claim_token=NULL,lease_until=NULL "
                "WHERE attempt_id=? AND processing_state='running' AND claim_token=? AND lease_until>?",
                (claim.attempt_id, claim.token, self.clock())
            )
            if cursor.rowcount:
                self._event(connection, claim.attempt_id, "released", {})
            return cursor.rowcount == 1

    def recover_expired(self) -> int:
        with self._write() as connection:
            rows = connection.execute(
                "SELECT a.attempt_id FROM documents d JOIN attempts a ON a.attempt_id=d.current_attempt_id "
                "WHERE a.processing_state='running' AND a.lease_until<=?", (self.clock(),)
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE attempts SET processing_state='pending',claim_token=NULL,lease_until=NULL WHERE attempt_id=?",
                    (row["attempt_id"],)
                )
                self._event(connection, row["attempt_id"], "recovered", {})
            return len(rows)

    def retry(self, document_id: str, *, settings_overrides: dict | None = None) -> int:
        """Create an explicit new attempt; preserve the finalized prior attempt."""
        with self._write() as connection:
            prior = connection.execute(
                "SELECT a.* FROM documents d JOIN attempts a ON a.attempt_id=d.current_attempt_id WHERE d.document_id=?",
                (document_id,)
            ).fetchone()
            if prior is None:
                raise ValueError("Unknown document; run intake first.")
            if prior["processing_state"] not in {"completed", "error"}:
                raise ValueError("Document has unfinished work; use resume instead of retry.")
            settings = json.loads(prior["settings_json"])
            settings.update(settings_overrides or {})
            encoded = canonical_json(settings)
            fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                "INSERT INTO attempts(document_id,source_sha256,source_bytes,settings_fingerprint,settings_json,"
                "processing_state,created_at) VALUES (?,?,?,?,?,'pending',?)",
                (document_id, prior["source_sha256"], prior["source_bytes"], fingerprint, encoded, self.clock())
            )
            attempt_id = cursor.lastrowid
            connection.execute("UPDATE documents SET current_attempt_id=? WHERE document_id=?", (attempt_id, document_id))
            self._event(connection, attempt_id, "retry-created", {"prior_attempt_id": prior["attempt_id"]})
            return attempt_id

    def request_delay(self, scope: str, *, interval_seconds: float) -> float:
        """Atomically admit a call or report wait time; shared across processes."""
        with self._write() as connection:
            now = self.clock()
            row = connection.execute("SELECT next_allowed FROM provider_rate_limits WHERE scope=?", (scope,)).fetchone()
            if row and row["next_allowed"] > now:
                return row["next_allowed"] - now
            connection.execute(
                "INSERT INTO provider_rate_limits(scope,next_allowed) VALUES (?,?) "
                "ON CONFLICT(scope) DO UPDATE SET next_allowed=excluded.next_allowed", (scope, now + interval_seconds)
            )
            return 0

    def defer_requests(self, scope: str, *, seconds: float):
        with self._write() as connection:
            connection.execute(
                "INSERT INTO provider_rate_limits(scope,next_allowed) VALUES (?,?) "
                "ON CONFLICT(scope) DO UPDATE SET next_allowed=MAX(next_allowed,excluded.next_allowed)",
                (scope, self.clock() + seconds)
            )

    def complete(self, claim: Claim, extraction: dict, response_source: str) -> bool:
        """Publish a validated extraction if this worker still owns current work."""
        check_routing_settings(claim.settings)
        message = message_from_export(json.loads(claim.source_bytes.decode("utf-8")))
        review = prepare_review(claim.document_id, extraction, message, schema=claim.settings["schema"])
        return self._finish(claim, "completed", review["extraction"], None, response_source, review)

    def fail(self, claim: Claim, error: dict, response_source: str) -> bool:
        route = {"status": "error", "owner": "IT Service Desk", "next_action": "retry-extraction"}
        return self._finish(claim, "error", None, error, response_source, route)

    def queue(self) -> list[dict]:
        """Current decisions linked to the same attempt/hash as extraction records.

        Reads persisted routes rather than applying today's policy to yesterday's
        extractions. Legacy completed records explicitly require a fresh intake.
        """
        items = []
        for record in self.current():
            item = {field: record[field] for field in (
                "document_id", "attempt_id", "source_sha256", "settings_fingerprint",
                "processing_state", "status", "owner", "next_action", "response_source", "error",
                "lease_until", "claim_count",
            )}
            extraction = record["extraction"]
            item["request_id"] = extraction["request_id"] if extraction is not None else None
            item["issues"] = extraction["issues"] if extraction is not None else []
            if item["status"] is None:
                state = item["processing_state"]
                if state in {"pending", "running"}:
                    status = "pending" if state == "pending" else "processing"
                    action = "await-extraction"
                    if state == "running" and record["lease_until"] <= self.clock():
                        status, action = "interrupted", "resume-processing"
                elif state == "error":
                    status, action = "error", "retry-extraction"
                else:
                    status, action = "refresh-required", "refresh-intake"
                item.update(status=status, owner="IT Service Desk", next_action=action)
            items.append(item)
        return items
