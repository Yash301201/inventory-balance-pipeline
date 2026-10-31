"""Movement aggregation, daily balance calculation, and independent reconciliation."""

import re
from decimal import Decimal
from collections import defaultdict
from datetime import date, timedelta


DAILY_FIELDS = [
    "run_id",
    "source_file",
    "source_sha256",
    "balance_date",
    "product_id",
    "location_id",
    "opening_quantity",
    "receipts",
    "issues",
    "transfers_in",
    "transfers_out",
    "adjustments",
    "net_change",
    "closing_quantity",
    "movement_count",
]
MOVEMENT_FIELDS = (
    "receipts",
    "issues",
    "transfers_in",
    "transfers_out",
    "adjustments",
    "movement_count",
)
MAX_CALENDAR_DAYS = 3660
MAX_OUTPUT_ROWS = 250_000


def compute_daily_balances(
    events: list[dict],
    run_id: str,
    source_file: str,
    source_sha256: str = "",
    max_calendar_days: int = MAX_CALENDAR_DAYS,
    max_output_rows: int = MAX_OUTPUT_ROWS,
) -> list[dict]:
    """Rebuild active event history into a bounded, dense daily balance grid."""
    if not events:
        return []

    ordered = sorted(events, key=lambda event: (event["event_date"], event["event_id"]))
    movements: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {
            "receipts": 0,
            "issues": 0,
            "transfers_in": 0,
            "transfers_out": 0,
            "adjustments": 0,
            "movement_count": 0,
        }
    )
    pairs: set[tuple[str, str]] = set()

    for event in ordered:
        event_type = event["event_type"]
        day = event["event_date"]
        product = event["product_id"]
        quantity = event["quantity"]
        if event_type == "transfer":
            affected = [
                (event["from_location"], "transfers_out"),
                (event["to_location"], "transfers_in"),
            ]
        elif event_type == "receipt":
            affected = [(event["location_id"], "receipts")]
        elif event_type == "issue":
            affected = [(event["location_id"], "issues")]
        else:
            affected = [(event["location_id"], "adjustments")]

        for location, category in affected:
            pairs.add((product, location))
            bucket = movements[(day, product, location)]
            bucket[category] += quantity
            bucket["movement_count"] += 1

    first_day = date.fromisoformat(ordered[0]["event_date"])
    last_day = max(date.fromisoformat(event["event_date"]) for event in ordered)
    calendar_days = (last_day - first_day).days + 1
    if calendar_days > max_calendar_days:
        raise ValueError(
            f"event date range spans {calendar_days} days; maximum is {max_calendar_days}"
        )
    output_rows = len(pairs) * calendar_days
    if output_rows > max_output_rows:
        raise ValueError(
            f"daily output would contain {output_rows} rows; maximum is {max_output_rows}"
        )
    # A range-based offset avoids incrementing date.max past its supported boundary.
    days = [
        (first_day + timedelta(days=offset)).isoformat()
        for offset in range(calendar_days)
    ]

    output: list[dict] = []
    empty_movement = {
        "receipts": 0,
        "issues": 0,
        "transfers_in": 0,
        "transfers_out": 0,
        "adjustments": 0,
        "movement_count": 0,
    }
    for product, location in sorted(pairs):
        balance = 0
        for day in days:
            # A non-mutating lookup avoids creating buckets for no-movement days.
            movement = movements.get((day, product, location), empty_movement)
            opening = balance
            receipts = movement["receipts"]
            issues = movement["issues"]
            transfers_in = movement["transfers_in"]
            transfers_out = movement["transfers_out"]
            adjustments = movement["adjustments"]
            net_change = receipts - issues + transfers_in - transfers_out + adjustments
            balance += net_change
            output.append(
                {
                    "run_id": run_id,
                    "source_file": source_file,
                    "source_sha256": source_sha256,
                    "balance_date": day,
                    "product_id": product,
                    "location_id": location,
                    "opening_quantity": opening,
                    "receipts": receipts,
                    "issues": issues,
                    "transfers_in": transfers_in,
                    "transfers_out": transfers_out,
                    "adjustments": adjustments,
                    "net_change": net_change,
                    "closing_quantity": balance,
                    "movement_count": movement["movement_count"],
                }
            )
    return output


def exact_integer(value):
    """Accept integers, integral finite floats/Decimals, and signed ASCII integer strings."""
    if isinstance(value, bool):
        raise ValueError("boolean is not an inventory integer")
    if isinstance(value, str):
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            raise ValueError("invalid integer syntax")
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        try:
            result = int(value)
        except (ValueError, OverflowError):
            raise ValueError("non-finite integer") from None
        if value == result:
            return result
    raise ValueError("non-integral inventory value")


def reconcile(
    events: list[dict],
    daily_rows: list[dict],
    *,
    input_rows: int | None = None,
    accepted_rows: int | None = None,
    rejected_rows: int | None = None,
) -> dict:
    """Compare movements, daily keys, continuity, transfers, totals, and row counts."""
    errors: list[str] = []
    expected_movements: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {
            "receipts": 0,
            "issues": 0,
            "transfers_in": 0,
            "transfers_out": 0,
            "adjustments": 0,
            "movement_count": 0,
        }
    )
    pairs: set[tuple[str, str]] = set()
    transfer_by_product_day: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"transfers_in": 0, "transfers_out": 0}
    )

    receipts = issues = adjustments = transfer_quantity = transfer_events = 0
    for event in events:
        day = event["event_date"]
        product = event["product_id"]
        event_type = event["event_type"]
        quantity = event["quantity"]
        if event_type == "transfer":
            transfer_events += 1
            transfer_quantity += quantity
            affected = [
                (event["from_location"], "transfers_out"),
                (event["to_location"], "transfers_in"),
            ]
            transfer_by_product_day[(day, product)]["transfers_out"] += quantity
            transfer_by_product_day[(day, product)]["transfers_in"] += quantity
        elif event_type == "receipt":
            receipts += quantity
            affected = [(event["location_id"], "receipts")]
        elif event_type == "issue":
            issues += quantity
            affected = [(event["location_id"], "issues")]
        else:
            adjustments += quantity
            affected = [(event["location_id"], "adjustments")]

        for location, category in affected:
            pairs.add((product, location))
            expected_movements[(day, product, location)][category] += quantity
            expected_movements[(day, product, location)]["movement_count"] += 1

    parsed_dates = [date.fromisoformat(event["event_date"]) for event in events]
    expected_keys: set[tuple[str, str, str]] = set()
    expected_dates: list[str] = []
    if parsed_dates:
        first_day = min(parsed_dates)
        last_day = max(parsed_dates)
        calendar_days = (last_day - first_day).days + 1
        expected_dates = [
            (first_day + timedelta(days=offset)).isoformat()
            for offset in range(calendar_days)
        ]
        expected_keys = {
            (day, product, location)
            for product, location in pairs
            for day in expected_dates
        }

    actual_by_key: dict[tuple[str, str, str], dict] = {}
    duplicate_keys: set[tuple[str, str, str]] = set()
    for row in daily_rows:
        key = (str(row["balance_date"]), str(row["product_id"]), str(row["location_id"]))
        if key in actual_by_key:
            duplicate_keys.add(key)
        else:
            actual_by_key[key] = row

    unique_keys_passed = not duplicate_keys
    if duplicate_keys:
        errors.append(f"daily output contains {len(duplicate_keys)} duplicate balance keys")

    coverage_passed = set(actual_by_key) == expected_keys
    if not coverage_passed:
        missing = len(expected_keys - set(actual_by_key))
        unexpected = len(set(actual_by_key) - expected_keys)
        errors.append(f"daily output coverage differs (missing={missing}, unexpected={unexpected})")

    movement_passed = True
    continuity_passed = True
    previous_closing: dict[tuple[str, str], int] = {}
    expected_ending = receipts - issues + adjustments
    max_date = max(expected_dates) if expected_dates else None
    actual_ending = 0

    for key in sorted(expected_keys):
        day, product, location = key
        row = actual_by_key.get(key)
        if row is None:
            movement_passed = False
            continuity_passed = False
            continue
        movement = expected_movements.get(
            key,
            {
                "receipts": 0,
                "issues": 0,
                "transfers_in": 0,
                "transfers_out": 0,
                "adjustments": 0,
                "movement_count": 0,
            },
        )
        try:
            actual_fields = {field: exact_integer(row[field]) for field in MOVEMENT_FIELDS}
            opening = exact_integer(row["opening_quantity"])
            closing = exact_integer(row["closing_quantity"])
            net_change = exact_integer(row["net_change"])
        except (KeyError, TypeError, ValueError):
            movement_passed = False
            continuity_passed = False
            errors.append(f"daily output has missing or non-integer values for {key}")
            continue

        if any(actual_fields[field] != movement[field] for field in MOVEMENT_FIELDS):
            movement_passed = False
            errors.append(f"movement totals do not match source events for {key}")

        prior_key = (product, location)
        expected_opening = previous_closing.get(prior_key, 0)
        if opening != expected_opening:
            continuity_passed = False
            errors.append(f"opening balance is discontinuous for {key}")
        expected_net = (
            movement["receipts"]
            - movement["issues"]
            + movement["transfers_in"]
            - movement["transfers_out"]
            + movement["adjustments"]
        )
        if net_change != expected_net or closing != opening + net_change:
            continuity_passed = False
            errors.append(f"balance equation failed for {key}")
        previous_closing[prior_key] = closing
        if day == max_date:
            actual_ending += closing

    actual_transfer_by_product_day: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"transfers_in": 0, "transfers_out": 0}
    )
    for row in daily_rows:
        try:
            transfer_key = (str(row["balance_date"]), str(row["product_id"]))
            actual_transfer_by_product_day[transfer_key]["transfers_in"] += exact_integer(row["transfers_in"])
            actual_transfer_by_product_day[transfer_key]["transfers_out"] += exact_integer(row["transfers_out"])
        except (KeyError, TypeError, ValueError):
            continue
    transfer_keys = set(transfer_by_product_day) | {
        key
        for key, totals in actual_transfer_by_product_day.items()
        if totals["transfers_in"] or totals["transfers_out"]
    }
    zero_transfer = {"transfers_in": 0, "transfers_out": 0}
    transfer_balance_passed = True
    for key in transfer_keys:
        expected_transfer = transfer_by_product_day.get(key, zero_transfer)
        actual_transfer = actual_transfer_by_product_day.get(key, zero_transfer)
        if (
            actual_transfer != expected_transfer
            or actual_transfer["transfers_in"] != actual_transfer["transfers_out"]
        ):
            transfer_balance_passed = False
            errors.append(f"transfer-in/out totals do not match source events for {key}")

    difference = actual_ending - expected_ending
    company_total_passed = difference == 0
    if not company_total_passed:
        errors.append(f"ending stock differs from source movements by {difference}")

    counts_available = (
        input_rows is not None and accepted_rows is not None and rejected_rows is not None
    )
    count_reconciliation_passed = (
        input_rows == accepted_rows + rejected_rows if counts_available else True
    )
    if counts_available and not count_reconciliation_passed:
        errors.append("source record count does not equal accepted plus rejected rows")

    checks = {
        "unique_balance_keys": unique_keys_passed,
        "expected_date_product_location_coverage": coverage_passed,
        "movement_categories_match_source": movement_passed,
        "opening_continuity_and_balance_equation": continuity_passed,
        "transfer_quantities_balance_by_product_date": transfer_balance_passed,
        "company_ending_total": company_total_passed,
        "count_reconciliation_passed": count_reconciliation_passed,
    }
    return {
        "receipts": receipts,
        "issues": issues,
        "transfer_events": transfer_events,
        "transfer_quantity": transfer_quantity,
        "adjustments": adjustments,
        "expected_ending_stock": expected_ending,
        "actual_ending_stock": actual_ending,
        "difference": difference,
        "checks": checks,
        "errors": errors,
        "passed": all(checks.values()),
    }
