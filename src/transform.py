"""Deterministic daily movement aggregation and stock balance calculation."""

from collections import defaultdict
from datetime import date, timedelta


DAILY_FIELDS = [
    "run_id",
    "source_file",
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


def compute_daily_balances(events: list[dict], run_id: str, source_file: str) -> list[dict]:
    """Rebuild balances from all accepted events, including historical corrections."""
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
                (event["from_location"], "transfers_out", -quantity),
                (event["to_location"], "transfers_in", quantity),
            ]
        elif event_type == "receipt":
            affected = [(event["location_id"], "receipts", quantity)]
        elif event_type == "issue":
            affected = [(event["location_id"], "issues", -quantity)]
        else:
            affected = [(event["location_id"], "adjustments", quantity)]

        for location, category, delta in affected:
            pair = (product, location)
            pairs.add(pair)
            bucket = movements[(day, product, location)]
            bucket[category] += quantity if category != "adjustments" else delta
            bucket["movement_count"] += 1

    first_day = date.fromisoformat(ordered[0]["event_date"])
    last_day = date.fromisoformat(ordered[-1]["event_date"])
    days: list[str] = []
    cursor = first_day
    while cursor <= last_day:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)

    output: list[dict] = []
    for product, location in sorted(pairs):
        balance = 0
        for day in days:
            movement = movements[(day, product, location)]
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


def reconcile(events: list[dict], daily_rows: list[dict]) -> dict:
    """Compare movement totals with the latest calculated stock total."""
    receipts = sum(e["quantity"] for e in events if e["event_type"] == "receipt")
    issues = sum(e["quantity"] for e in events if e["event_type"] == "issue")
    adjustments = sum(e["quantity"] for e in events if e["event_type"] == "adjustment")
    expected = receipts - issues + adjustments

    latest_by_pair: dict[tuple[str, str], dict] = {}
    for row in daily_rows:
        latest_by_pair[(row["product_id"], row["location_id"])] = row
    actual = sum(row["closing_quantity"] for row in latest_by_pair.values())
    difference = actual - expected
    return {
        "receipts": receipts,
        "issues": issues,
        "transfer_events": sum(e["event_type"] == "transfer" for e in events),
        "adjustments": adjustments,
        "expected_ending_stock": expected,
        "actual_ending_stock": actual,
        "difference": difference,
        "passed": difference == 0,
    }
