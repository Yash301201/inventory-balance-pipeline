"""Create deterministic clean inventory data or a separately labeled anomaly fixture."""

import argparse
import csv
import hashlib
import json
import random
from datetime import date, timedelta
from pathlib import Path

from validate import TRANSACTION_FIELDS, load_reference_keys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LABEL_FIELDS = ["source_record", "scenario_id", "expected_disposition", "expected_reason_code"]
EXPECTED_BALANCE_FIELDS = ["balance_date", "product_id", "location_id", "closing_quantity"]


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _reference_values(path: Path, key_field: str) -> list[str]:
    values = sorted(load_reference_keys(path, key_field))
    if not values:
        raise ValueError(f"{path} contains no {key_field} keys")
    return values


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def generate(
    rows: int,
    seed: int,
    output: Path,
    products_path: Path | None = None,
    locations_path: Path | None = None,
) -> None:
    """Generate a clean, reproducible baseline using the supplied master files."""
    if rows < 0:
        raise ValueError("--rows must be zero or greater")
    products_path = resolve_path(products_path or Path("data/input/products.csv"))
    locations_path = resolve_path(locations_path or Path("data/input/locations.csv"))
    products = _reference_values(products_path, "product_id")
    locations = _reference_values(locations_path, "location_id")
    if len(locations) < 2:
        raise ValueError("the baseline generator needs at least two location IDs")
    output = resolve_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    start = date(2026, 1, 1)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
        writer.writeheader()
        for index in range(rows):
            event_type = rng.choices(
                ["receipt", "issue", "transfer", "adjustment"],
                weights=[40, 30, 15, 15],
                k=1,
            )[0]
            day = start + timedelta(days=rng.randrange(365))
            product = rng.choice(products)
            quantity = rng.randint(1, 20)
            row = {
                "event_id": f"GEN-{index + 1:09d}",
                "event_date": day.isoformat(),
                "product_id": product,
                "event_type": event_type,
                "quantity": quantity,
                "location_id": "",
                "from_location": "",
                "to_location": "",
                "recorded_at": "",
                "supersedes_event_id": "",
            }
            if event_type in {"receipt", "issue"}:
                row["location_id"] = rng.choice(locations)
            elif event_type == "adjustment":
                row["location_id"] = rng.choice(locations)
                row["quantity"] = quantity if rng.choice([True, False]) else -quantity
            else:
                source, target = rng.sample(locations, 2)
                row["from_location"] = source
                row["to_location"] = target
            writer.writerow(row)


def generate_scenario(
    output: Path,
    labels_output: Path | None = None,
    manifest_output: Path | None = None,
    expected_output: Path | None = None,
    products_path: Path | None = None,
    locations_path: Path | None = None,
) -> dict:
    """Write deterministic correction/anomaly events and separate ground truth."""
    products_path = resolve_path(products_path or Path("data/input/products.csv"))
    locations_path = resolve_path(locations_path or Path("data/input/locations.csv"))
    product_ids = _reference_values(products_path, "product_id")
    product_id = product_ids[0]
    unknown_product = "__UNKNOWN_PRODUCT__"
    while unknown_product in product_ids:
        unknown_product += "_"
    location_id = _reference_values(locations_path, "location_id")[0]
    output = resolve_path(output)
    labels_output = resolve_path(labels_output or output.with_suffix(".labels.csv"))
    manifest_output = resolve_path(manifest_output or output.with_suffix(".manifest.json"))
    expected_output = resolve_path(
        expected_output or output.with_suffix(".expected_balances.csv")
    )

    def event(
        event_id: str,
        event_date: str,
        event_type: str,
        quantity: str | int,
        location: str = location_id,
        product: str = product_id,
        recorded_at: str = "",
        supersedes: str = "",
    ) -> dict:
        return {
            "event_id": event_id,
            "event_date": event_date,
            "product_id": product,
            "event_type": event_type,
            "quantity": quantity,
            "location_id": location,
            "from_location": "",
            "to_location": "",
            "recorded_at": recorded_at,
            "supersedes_event_id": supersedes,
        }

    original = event("SC-RECEIPT-V1", "2026-01-01", "receipt", 10)
    issue = event("SC-ISSUE-1", "2026-01-02", "issue", 3)
    correction = event(
        "SC-RECEIPT-V2",
        "2026-01-01",
        "receipt",
        8,
        recorded_at="2026-01-03T10:00:00Z",
        supersedes="SC-RECEIPT-V1",
    )
    rows_with_labels = [
        (original, "correction_chain", "accepted_superseded", ""),
        (issue, "late_history", "accepted_active", ""),
        (correction, "audited_correction", "accepted_active", ""),
        (issue.copy(), "identical_redelivery", "rejected", "identical_redelivery"),
        (event("SC-ISSUE-1", "2026-01-02", "issue", 4), "conflicting_event_id", "rejected", "conflicting_event_id"),
        (event("SC-UNKNOWN", "2026-01-01", "receipt", 1, product=unknown_product), "unknown_product", "rejected", "unknown_product"),
        (event("SC-MISSING-LOCATION", "2026-01-01", "receipt", 1, location=""), "missing_value", "rejected", "missing_location"),
        (event("SC-BAD-QUANTITY", "2026-01-01", "receipt", "1.5"), "invalid_quantity", "rejected", "invalid_quantity"),
        (event("SC-BAD-DATE", "not-a-date", "receipt", 1), "invalid_date", "rejected", "invalid_event_date"),
        (event("SC-ADJUST-UP", "2026-01-04", "adjustment", 1), "quantity_boundary", "accepted_active", ""),
        (event("SC-ADJUST-DOWN", "2026-01-06", "adjustment", -1), "quantity_boundary", "accepted_active", ""),
    ]
    event_rows = [row for row, _scenario, _disposition, _reason in rows_with_labels]
    _write_csv(output, TRANSACTION_FIELDS, event_rows)
    _write_csv(
        labels_output,
        LABEL_FIELDS,
        [
            {
                "source_record": source_record,
                "scenario_id": scenario,
                "expected_disposition": disposition,
                "expected_reason_code": reason_code,
            }
            for source_record, (_row, scenario, disposition, reason_code)
            in enumerate(rows_with_labels, start=1)
        ],
    )

    # Hand-calculated fixture oracle: correction 10 -> 8, then issue 3; +1 and -1 adjustments.
    expected_rows = []
    closing = 0
    movements = {
        "2026-01-01": 8,
        "2026-01-02": -3,
        "2026-01-04": 1,
        "2026-01-06": -1,
    }
    for offset in range(6):
        day = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
        closing += movements.get(day, 0)
        expected_rows.append(
            {
                "balance_date": day,
                "product_id": product_id,
                "location_id": location_id,
                "closing_quantity": closing,
            }
        )
    _write_csv(expected_output, EXPECTED_BALANCE_FIELDS, expected_rows)

    source_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    label_hash = hashlib.sha256(labels_output.read_bytes()).hexdigest()
    expected_hash = hashlib.sha256(expected_output.read_bytes()).hexdigest()
    scenario_counts = {}
    for _row, scenario, _disposition, _reason in rows_with_labels:
        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
    manifest = {
        "version": 1,
        "fixture": output.name,
        "seed": "fixed-scenario-v1",
        "rows": len(event_rows),
        "sha256": source_hash,
        "labels_file": labels_output.name,
        "labels_sha256": label_hash,
        "expected_balances_file": expected_output.name,
        "expected_balances_sha256": expected_hash,
        "scenario_counts": scenario_counts,
        "expected_dispositions": {
            disposition: sum(item[2] == disposition for item in rows_with_labels)
            for disposition in sorted({item[2] for item in rows_with_labels})
        },
        "correction_acceptance_example": {
            "original_receipt": 10,
            "corrected_receipt": 8,
            "issue": 3,
            "expected_closing_after_jan_1": 8,
            "expected_closing_after_jan_2": 5,
            "correction_reprocessed_twice": "one active receipt version",
        },
        "labels_are_not_pipeline_input": True,
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "rows": len(event_rows),
        "sha256": source_hash,
        "events_path": output,
        "labels_path": labels_output,
        "expected_path": expected_output,
        "manifest_path": manifest_output,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("data/input/large_transactions.csv"))
    parser.add_argument("--products", type=Path, default=Path("data/input/products.csv"))
    parser.add_argument("--locations", type=Path, default=Path("data/input/locations.csv"))
    parser.add_argument("--scenario-output", type=Path)
    args = parser.parse_args()
    generate(args.rows, args.seed, args.output, args.products, args.locations)
    output = resolve_path(args.output)
    print(f"Wrote {args.rows:,} deterministic events to {output}")
    if args.scenario_output:
        fixture = generate_scenario(
            args.scenario_output,
            products_path=args.products,
            locations_path=args.locations,
        )
        print(
            f"Wrote {fixture['rows']} deterministic scenario events to {fixture['events_path']}; "
            f"labels: {fixture['labels_path']}; expected balances: {fixture['expected_path']}; "
            f"manifest: {fixture['manifest_path']}"
        )


if __name__ == "__main__":
    main()
