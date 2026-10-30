"""Run-scoped artifact staging, immutable input archives, and success-pointer commit."""

import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def logical_source_path(path: Path, project_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def archive_inputs(
    stage_dir: Path,
    inputs: dict[str, Path],
    project_root: Path,
) -> dict:
    """Copy the exact bytes used for a run into its isolated staging directory."""
    snapshot_dir = stage_dir / "input_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    sources = {}
    for role, source_path in inputs.items():
        destination_name = f"{role}{source_path.suffix.lower() or '.csv'}"
        destination = snapshot_dir / destination_name
        shutil.copyfile(source_path, destination)
        sources[role] = {
            "logical_path": logical_source_path(source_path, project_root),
            "snapshot_path": f"input_snapshot/{destination_name}",
            "sha256": sha256_file(destination),
            "size_bytes": destination.stat().st_size,
        }
    return sources


def write_json_artifact(path: Path, payload: dict) -> None:
    """Write JSON within a private run directory."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact_index(run_dir: Path) -> dict[str, dict]:
    """Hash every completed run artifact except the manifest being written."""
    artifacts = {}
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.name != "run_manifest.json":
            relative = path.relative_to(run_dir).as_posix()
            artifacts[relative] = {
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
    return artifacts


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary_path, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def publish_latest_success(
    output_dir: Path,
    run_id: str,
    manifest_relative_path: str,
    published_row_counts: dict[str, int],
) -> dict:
    """Atomically switch consumers to one complete, already validated run."""
    pointer = {
        "version": 1,
        "run_id": run_id,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest_relative_path,
        "published_row_counts": published_row_counts,
    }
    _atomic_json(output_dir / "latest_success.json", pointer)
    return pointer
