"""Run the local, auditable inventory balance pipeline."""

import argparse
import csv
import json
import logging
import platform
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from publish import (
    OutputLock,
    archive_inputs,
    artifact_index,
    publish_latest_success,
    sha256_file,
    write_json_artifact,
)
from transform import DAILY_FIELDS, compute_daily_balances, reconcile
from validate import InputSchemaError, read_and_validate, validate_event_history


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REJECT_FIELDS = [
    "run_id", "source_file", "source_sha256", "source_record", "line_start",
    "line_end", "reason_code", "reason", "raw_event", "extra_values",
]
ACCEPTED_FIELDS = [
    "run_id", "source_file", "source_sha256", "source_record", "line_start",
    "line_end", "event_id", "event_date", "recorded_at", "product_id",
    "event_type", "quantity", "location_id", "from_location", "to_location",
    "supersedes_event_id", "version_status",
]


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()


def load_previous_event_versions(output_dir: Path) -> list[dict]:
    """Load accepted IDs from the prior successful run referenced by the commit pointer."""
    pointer_path = output_dir / "latest_success.json"
    if not pointer_path.exists():
        return []
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        manifest_path = (output_dir / pointer["manifest"]).resolve()
        manifest_path.relative_to(output_dir.resolve())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        accepted_path = manifest_path.parent / "accepted_events.csv"
        expected_hash = manifest["artifacts"]["accepted_events.csv"]["sha256"]
        if sha256_file(accepted_path) != expected_hash:
            raise InputSchemaError("previous successful accepted_events.csv failed its SHA-256 check")
        with accepted_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames != ACCEPTED_FIELDS:
                raise InputSchemaError("previous successful run has an invalid accepted_events.csv header")
            previous = list(reader)
            if any(None in row for row in previous):
                raise InputSchemaError("previous successful run has malformed accepted event rows")
            return previous
    except (KeyError, OSError, ValueError, csv.Error) as error:
        if isinstance(error, InputSchemaError):
            raise
        raise InputSchemaError(f"cannot read prior successful event history: {error}") from error


def _serialize_rejects(run_id: str, rejected: list[dict]) -> list[dict]:
    return [
        {
            "run_id": run_id,
            "source_file": item["source_file"],
            "source_sha256": item["source_sha256"],
            "source_record": item["source_record"],
            "line_start": item["line_start"],
            "line_end": item["line_end"],
            "reason_code": item["reason_code"],
            "reason": item["reason"],
            "raw_event": json.dumps(item["raw_event"], sort_keys=True, ensure_ascii=False),
            "extra_values": json.dumps(item["extra_values"], ensure_ascii=False),
        }
        for item in rejected
    ]


def _serialize_events(run_id: str, events: list[dict]) -> list[dict]:
    return [
        {field: event.get(field, "") for field in ACCEPTED_FIELDS if field != "run_id"}
        | {"run_id": run_id}
        for event in events
    ]


def verify_persisted_artifacts(stage_dir, run_id, events, rejected, daily_rows, active_events, input_rows):
    """Read serialized content independently before hashing and activation."""
    specs = [
        ("daily_balances.csv", DAILY_FIELDS, daily_rows),
        ("accepted_events.csv", ACCEPTED_FIELDS, _serialize_events(run_id, events)),
        ("rejected_records.csv", REJECT_FIELDS, _serialize_rejects(run_id, rejected)),
    ]
    persisted_daily = []
    for name, fields, expected in specs:
        with (stage_dir / name).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames != fields:
                raise RuntimeError(f"persisted artifact header mismatch: {name}")
            count = 0
            for count, row in enumerate(reader, 1):
                if count > len(expected) or row != {field: str(expected[count - 1][field]) for field in fields}:
                    raise RuntimeError(f"persisted artifact content mismatch: {name} row {count}")
                if row["run_id"] != run_id:
                    raise RuntimeError(f"persisted artifact run ID mismatch: {name}")
                if name == "daily_balances.csv":
                    persisted_daily.append(row)
            if count != len(expected):
                raise RuntimeError(f"persisted artifact count mismatch: {name}")
    result = reconcile(active_events, persisted_daily, input_rows=input_rows,
                       accepted_rows=len(events), rejected_rows=len(rejected))
    if not result["passed"]:
        raise RuntimeError("persisted daily reconciliation failed: " + "; ".join(result["errors"]))


def _update_summary_counts(summary: dict, counts: dict) -> None:
    summary["input_rows"] = counts.get("input_rows", 0)
    summary["accepted_rows"] = counts.get("accepted_rows", 0)
    summary["rejected_rows"] = counts.get("rejected_rows", 0)
    summary["active_event_rows"] = counts.get("active_event_rows", 0)
    summary["superseded_event_rows"] = counts.get("superseded_event_rows", 0)
    summary["calculated_balance_rows"] = counts.get("calculated_balance_rows", 0)
    summary["prepared_balance_rows"] = counts.get("prepared_balance_rows", 0)
    summary["daily_balance_rows"] = counts.get("calculated_balance_rows", 0)


def run_pipeline(
    input_path: Path,
    products_path: Path,
    locations_path: Path,
    output_dir: Path,
) -> int:
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    start_clock = time.perf_counter()
    output_dir = Path(output_dir)
    runs_dir = output_dir / "runs"
    stage_dir = runs_dir / f".staging-{run_id}"
    final_run_dir = runs_dir / run_id
    current_stage = "prepare"
    counts = {
        "input_rows": 0,
        "accepted_rows": 0,
        "rejected_rows": 0,
        "active_event_rows": 0,
        "superseded_event_rows": 0,
        "calculated_balance_rows": 0,
        "prepared_balance_rows": 0,
    }
    summary = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "status": "failed",
        "data_quality_status": "unknown",
        "publication_status": "not_published",
        "input_rows": 0,
        "accepted_rows": 0,
        "rejected_rows": 0,
        "active_event_rows": 0,
        "superseded_event_rows": 0,
        "calculated_balance_rows": 0,
        "prepared_balance_rows": 0,
        "daily_balance_rows": 0,
        "reject_rate": None,
        "count_reconciliation_passed": None,
        "sources": None,
        "reconciliation": None,
        "elapsed_ms": None,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "error": None,
    }

    writer_lock = OutputLock(output_dir)
    try:
        current_stage = "acquire_writer_lock"
        writer_lock.acquire()
        output_dir.mkdir(parents=True, exist_ok=True)
        runs_dir.mkdir(parents=True, exist_ok=True)
        stage_dir.mkdir(parents=False, exist_ok=False)
        current_stage = "snapshot_inputs"
        sources = archive_inputs(
            stage_dir,
            {
                "transactions": Path(input_path),
                "products": Path(products_path),
                "locations": Path(locations_path),
            },
            PROJECT_ROOT,
        )
        summary["sources"] = sources
        transaction_source = sources["transactions"]
        logical_transaction_path = transaction_source["logical_path"]
        transaction_sha256 = transaction_source["sha256"]

        current_stage = "validate"
        snapshot_dir = stage_dir / "input_snapshot"
        events, rejected, input_rows = read_and_validate(
            snapshot_dir / f"transactions{Path(input_path).suffix.lower() or '.csv'}",
            snapshot_dir / f"products{Path(products_path).suffix.lower() or '.csv'}",
            snapshot_dir / f"locations{Path(locations_path).suffix.lower() or '.csv'}",
            source_file=logical_transaction_path,
            source_sha256=transaction_sha256,
            progress=counts,
        )
        sources["transactions"]["record_count"] = input_rows
        summary["sources"] = sources
        counts["input_rows"] = input_rows
        counts["accepted_rows"] = len(events)
        counts["rejected_rows"] = len(rejected)
        current_stage = "validate_history"
        previous_events = load_previous_event_versions(output_dir)
        validate_event_history(previous_events, events)
        active_events = [event for event in events if event["version_status"] == "active"]
        counts["active_event_rows"] = len(active_events)
        counts["superseded_event_rows"] = len(events) - len(active_events)
        _update_summary_counts(summary, counts)

        current_stage = "transform"
        daily_rows = compute_daily_balances(
            active_events,
            run_id,
            logical_transaction_path,
            transaction_sha256,
        )
        counts["calculated_balance_rows"] = len(daily_rows)
        _update_summary_counts(summary, counts)

        current_stage = "reconcile"
        reconciliation = reconcile(
            active_events,
            daily_rows,
            input_rows=input_rows,
            accepted_rows=len(events),
            rejected_rows=len(rejected),
        )
        summary["reconciliation"] = reconciliation
        summary["count_reconciliation_passed"] = reconciliation["checks"][
            "count_reconciliation_passed"
        ]
        if not reconciliation["passed"]:
            raise RuntimeError(
                "reconciliation failed: " + "; ".join(reconciliation["errors"])
            )

        current_stage = "write_artifacts"
        write_csv(stage_dir / "daily_balances.csv", DAILY_FIELDS, daily_rows)
        write_csv(
            stage_dir / "rejected_records.csv",
            REJECT_FIELDS,
            _serialize_rejects(run_id, rejected),
        )
        write_csv(
            stage_dir / "accepted_events.csv",
            ACCEPTED_FIELDS,
            _serialize_events(run_id, events),
        )
        counts["prepared_balance_rows"] = len(daily_rows)
        _update_summary_counts(summary, counts)

        current_stage = "verify_persisted_artifacts"
        verify_persisted_artifacts(stage_dir, run_id, events, rejected, daily_rows, active_events, input_rows)
        summary["artifact_state"] = "prepared"
        summary["timing_scope"] = "start through persisted artifact verification; excludes summary, manifest, hashing, promotion and pointer commit"
        summary.update(
            {
                "status": "success_with_rejects" if rejected else "success",
                "data_quality_status": "has_rejects" if rejected else "clean",
                "publication_status": "prepared",
                "reject_rate": round(len(rejected) / input_rows, 6) if input_rows else 0.0,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": round((time.perf_counter() - start_clock) * 1000, 2),
                "error": None,
            }
        )
        current_stage = "write_summary"
        write_json_artifact(stage_dir / "run_summary.json", summary)

        current_stage = "build_manifest"
        manifest = {
            "version": 1,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_files": sources,
            "input_mode": "complete_historical_snapshot",
            "run_status": summary["status"],
            "publication_status": "prepared",
            "artifact_state": "prepared",
            "counts": {
                "input_rows": input_rows,
                "accepted_event_versions": len(events),
                "active_events": len(active_events),
                "superseded_event_versions": len(events) - len(active_events),
                "rejected_rows": len(rejected),
                "daily_balance_rows": len(daily_rows),
            },
            "count_reconciliation_passed": summary["count_reconciliation_passed"],
            "reconciliation_passed": reconciliation["passed"],
            "artifacts": artifact_index(stage_dir),
        }
        write_json_artifact(stage_dir / "run_manifest.json", manifest)

        required_artifacts = {
            "daily_balances.csv",
            "rejected_records.csv",
            "accepted_events.csv",
            "run_summary.json",
            "run_manifest.json",
        } | {source["snapshot_path"] for source in sources.values()}
        actual_artifacts = {
            path.relative_to(stage_dir).as_posix()
            for path in stage_dir.rglob("*")
            if path.is_file()
        }
        if not required_artifacts.issubset(actual_artifacts):
            missing = sorted(required_artifacts - actual_artifacts)
            raise RuntimeError(f"run is missing required artifacts: {missing}")

        current_stage = "publish_run_directory"
        stage_dir.replace(final_run_dir)
        current_stage = "publish_latest_success"
        publish_latest_success(
            output_dir,
            run_id,
            f"runs/{run_id}/run_manifest.json",
            {
                "daily_balances": len(daily_rows),
                "accepted_events": len(events),
                "rejected_records": len(rejected),
            },
        )
        logging.info(
            "run_id=%s input=%d accepted=%d rejected=%d active=%d balance_rows=%d",
            run_id,
            input_rows,
            len(events),
            len(rejected),
            len(active_events),
            len(daily_rows),
        )
        return 0
    except Exception as error:
        latest_pointer = output_dir / "latest_success.json"
        try:
            if latest_pointer.exists():
                published = json.loads(latest_pointer.read_text(encoding="utf-8"))
                if published.get("run_id") == run_id:
                    logging.error(
                        "run_id=%s raised after its success pointer was committed: %s",
                        run_id,
                        error,
                    )
                    return 0
        except (OSError, ValueError):
            pass
        _update_summary_counts(summary, counts)
        summary.update(
            {
                "status": "failed",
                "data_quality_status": (
                    "has_rejects" if counts.get("rejected_rows", 0) else "unknown"
                ),
                "publication_status": "not_published",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": round((time.perf_counter() - start_clock) * 1000, 2),
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                    "stage": current_stage,
                },
            }
        )
        logging.exception("run_id=%s stage=%s failed", run_id, current_stage)

        diagnostic_dir = final_run_dir if final_run_dir.exists() else stage_dir
        try:
            if diagnostic_dir.exists():
                write_json_artifact(diagnostic_dir / "run_summary.json", summary)
                manifest_path = diagnostic_dir / "run_manifest.json"
                if manifest_path.exists():
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        manifest["run_status"] = "failed"
                        manifest["publication_status"] = "not_published"
                        manifest["error"] = summary["error"]
                        manifest["artifacts"] = artifact_index(diagnostic_dir)
                        write_json_artifact(manifest_path, manifest)
                    except Exception as manifest_error:
                        logging.error(
                            "run_id=%s could not refresh failure manifest: %s: %s",
                            run_id,
                            type(manifest_error).__name__,
                            manifest_error,
                        )
                failed_dir = runs_dir / f"failed-{run_id}"
                if diagnostic_dir != failed_dir:
                    diagnostic_dir.replace(failed_dir)
        except Exception as diagnostic_error:
            logging.error(
                "run_id=%s could not persist failure summary: %s: %s",
                run_id,
                type(diagnostic_error).__name__,
                diagnostic_error,
            )
            print(
                f"inventory pipeline failure summary could not be written: "
                f"{type(diagnostic_error).__name__}: {diagnostic_error}",
                file=sys.stderr,
            )
        return 1
    finally:
        writer_lock.release()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/input/transactions.csv")
    parser.add_argument("--products", default="data/input/products.csv")
    parser.add_argument("--locations", default="data/input/locations.csv")
    parser.add_argument("--output-dir", default="data/output")
    args = parser.parse_args()
    return run_pipeline(
        resolve_path(args.input),
        resolve_path(args.products),
        resolve_path(args.locations),
        resolve_path(args.output_dir),
    )


if __name__ == "__main__":
    sys.exit(main())
