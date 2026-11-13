"""Consistent POSIX snapshot exports: one database read, one atomic pointer switch."""
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from it_access.extraction import SCHEMA_PATH, _check_schema, message_from_export, validate_response
from it_access.providers import ModelResponse
from it_access.storage import Store, canonical_json


FILES = ("extractions.jsonl", "queue.json")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _config(settings):
    return {
        **{key: settings.get(key) for key in ("provider", "model", "generation", "pipeline_version", "routing_version", "provider_config")},
        "prompt_sha256": _sha(settings["prompt"].encode()), "policy_sha256": _sha(settings["policy"].encode()),
        "schema_sha256": _sha(canonical_json(settings["schema"]).encode()),
        "mock_reply_sha256": _sha(settings["mock_reply"].encode()) if isinstance(settings.get("mock_reply"), str) else None,
    }


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, raw):
    with path.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_current(output, target):
    temporary = output / f".current-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, output / ".current")
        _fsync_directory(output)
    finally:
        temporary.unlink(missing_ok=True)


def export_snapshot(store: Store, output: str | Path) -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".export.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = store.current()  # Exactly one consistent SELECT for both outputs.
        if not current or any(row["processing_state"] not in {"completed", "error"} or row["status"] is None for row in current):
            raise ValueError("Export requires nonempty finalized/routed current records; resume unfinished work or refresh legacy intake first.")
        records = []
        for row in current:
            records.append({**{field: row[field] for field in (
                "document_id", "attempt_id", "source_sha256", "settings_fingerprint", "processing_state",
                "extraction", "status", "owner", "next_action", "error", "response_source",
            )}, "processing_config": _config(row["settings"])})
        items = store.queue_from_records(current)
        snapshot_id = _sha(canonical_json({"records": records, "queue": items}).encode())
        for row in records + items:
            row["snapshot_id"] = snapshot_id
        data = {
            "extractions.jsonl": ("".join(canonical_json(row) + "\n" for row in records)).encode(),
            "queue.json": (json.dumps({"snapshot_id": snapshot_id, "items": items}, indent=2, ensure_ascii=False) + "\n").encode(),
        }
        manifest = {"format_version": 1, "snapshot_id": snapshot_id, "documents": len(records),
                    "files": {name: _sha(raw) for name, raw in data.items()}}
        data["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
        for name in FILES:
            path = output / name
            if os.path.lexists(path) and (not path.is_symlink() or os.readlink(path) != f".current/{name}"):
                raise ValueError("Output file is not a managed snapshot link; choose a new output directory or relocate it first.")
        if os.path.lexists(output / ".current") and not (output / ".current").is_symlink():
            raise ValueError("Output .current must be a managed snapshot symlink.")
        snapshots = output / ".snapshots"
        snapshots.mkdir(exist_ok=True)
        destination = snapshots / snapshot_id
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=snapshots))
            try:
                for name, raw in data.items():
                    _write(staging / name, raw)
                _fsync_directory(staging)
                staging.rename(destination)
                _fsync_directory(snapshots)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        elif any((destination / name).read_bytes() != raw for name, raw in data.items()):
            raise ValueError("Existing immutable snapshot contents differ; inspect possible corruption.")
        for name in FILES:
            path = output / name
            if not os.path.lexists(path):
                path.symlink_to(f".current/{name}")
        _publish_current(output, f".snapshots/{snapshot_id}")
        return {"snapshot_id": snapshot_id, "documents": len(records), "directory": str(destination)}


def verify_export(output: str | Path, *, sources_dir: str | Path | None = None) -> dict:
    output = Path(output)
    # Resolve the pointer once: later publication cannot change this pinned pair.
    directory = (output / ".current").resolve(strict=True)
    if directory.parent != (output / ".snapshots").resolve():
        raise ValueError("Snapshot pointer must resolve inside this output's .snapshots directory.")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format_version") != 1 or manifest.get("snapshot_id") != directory.name:
        raise ValueError("Invalid snapshot manifest identity.")
    for name in FILES:
        if _sha((directory / name).read_bytes()) != manifest["files"][name]:
            raise ValueError("Snapshot file hash mismatch.")
        if not (output / name).is_symlink() or os.readlink(output / name) != f".current/{name}":
            raise ValueError("Current outputs must share the managed snapshot pointer.")
    records = [json.loads(line) for line in (directory / "extractions.jsonl").read_text().splitlines()]
    queue = json.loads((directory / "queue.json").read_text())
    if queue.get("snapshot_id") != manifest["snapshot_id"]:
        raise ValueError("Queue and manifest snapshot identities differ.")
    items = {item["document_id"]: item for item in queue["items"]}
    names = [row["document_id"] for row in records]
    if not records or len(names) != len(set(names)) or len(items) != len(queue["items"]) or set(names) != set(items) or len(names) != manifest["documents"]:
        raise ValueError("Snapshots must contain one linked current record per document.")
    schema = json.loads(SCHEMA_PATH.read_text())
    for record in records:
        name = record["document_id"]
        if Path(name).name != name:
            raise ValueError("Document identity must be a filename.")
        item = items[name]
        if record["snapshot_id"] != manifest["snapshot_id"]:
            raise ValueError("Extraction snapshot identity mismatch.")
        for field in ("snapshot_id", "attempt_id", "source_sha256", "settings_fingerprint", "processing_state", "status", "owner", "next_action", "error", "response_source"):
            if item[field] != record[field]:
                raise ValueError("Queue/extraction source, attempt or decision mismatch.")
        if not isinstance(record["response_source"], str) or not record["response_source"]:
            raise ValueError("Missing response provenance.")
        if record["status"] == "error":
            if record["extraction"] is not None or not record["error"] or (record["owner"], record["next_action"]) != ("IT Service Desk", "retry-extraction"):
                raise ValueError("Invalid current error record.")
        elif record["status"] in {"review", "needs-information"}:
            if record["error"] is not None or record["processing_state"] != "completed":
                raise ValueError("Successful record has inconsistent completion/error state.")
            _check_schema(record["extraction"], schema)
            if item["request_id"] != record["extraction"]["request_id"] or item["issues"] != record["extraction"]["issues"]:
                raise ValueError("Queue facts differ from the linked extraction.")
        else:
            raise ValueError("Unfinished or non-policy status in finalized export.")
        if sources_dir is not None:
            raw = (Path(sources_dir) / name).read_bytes()
            if _sha(raw) != record["source_sha256"]:
                raise ValueError("Export source hash differs from the supplied original bytes.")
            if record["extraction"] is not None:
                message = message_from_export(json.loads(raw.decode("utf-8")))
                validate_response(ModelResponse(json.dumps(record["extraction"]), "completed", record["response_source"]), message)
    return {"snapshot_id": manifest["snapshot_id"], "documents": len(records),
            "response_sources": sorted({record["response_source"] for record in records}),
            "sources_checked": sources_dir is not None, "verified": True}
