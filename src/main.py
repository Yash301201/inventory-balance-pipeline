"""Run the local inventory balance pipeline."""

import argparse
import csv
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from transform import DAILY_FIELDS, compute_daily_balances, reconcile
from validate import InputSchemaError, read_and_validate


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REJECT_FIELDS = ["run_id", "source_file", "source_row", "reason", "raw_event"]


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_pipeline(input_path: Path, products_path: Path, locations_path: Path, output_dir: Path) -> int:
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    start_clock = time.perf_counter()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "run_summary.json"
    summary = {
        "run_id": run_id,
        "source_file": input_path.name,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "status": "failed",
        "input_rows": 0,
        "accepted_rows": 0,
        "rejected_rows": 0,
        "daily_balance_rows": 0,
        "reconciliation": None,
        "elapsed_ms": None,
        "error": None,
    }
    try:
        events, rejected, input_rows = read_and_validate(input_path, products_path, locations_path)
        daily_rows = compute_daily_balances(events, run_id, input_path.name)
        reconciliation = reconcile(events, daily_rows)
        if not reconciliation["passed"]:
            raise RuntimeError(f"reconciliation difference is {reconciliation['difference']}")

        reject_rows = [
            {
                "run_id": run_id,
                "source_file": item["source_file"],
                "source_row": item["source_row"],
                "reason": item["reason"],
                "raw_event": json.dumps(item["raw_event"], sort_keys=True),
            }
            for item in rejected
        ]
        write_csv(output_dir / "daily_balances.csv", DAILY_FIELDS, daily_rows)
        write_csv(output_dir / "rejected_records.csv", REJECT_FIELDS, reject_rows)
        summary.update(
            {
                "status": "success",
                "input_rows": input_rows,
                "accepted_rows": len(events),
                "rejected_rows": len(rejected),
                "daily_balance_rows": len(daily_rows),
                "reconciliation": reconciliation,
            }
        )
        logging.info(
            "run_id=%s input=%d accepted=%d rejected=%d balance_rows=%d",
            run_id,
            input_rows,
            len(events),
            len(rejected),
            len(daily_rows),
        )
        return_code = 0
    except (OSError, InputSchemaError, RuntimeError, csv.Error) as error:
        summary["error"] = str(error)
        logging.exception("run_id=%s failed", run_id)
        return_code = 1
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["elapsed_ms"] = round((time.perf_counter() - start_clock) * 1000, 2)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return return_code


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
