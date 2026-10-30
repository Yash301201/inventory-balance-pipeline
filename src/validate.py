"""Input and business-rule validation for inventory events."""

import csv
from pathlib import Path


TRANSACTION_FIELDS = [
    "event_id",
    "event_date",
    "product_id",
    "event_type",
    "quantity",
    "location_id",
    "from_location",
    "to_location",
]


class InputSchemaError(ValueError):
    """Raised when a CSV's header does not match the documented schema."""


def load_reference_keys(path: Path, key_field: str) -> set[str]:
    """Read a simple reference CSV and return its unique, non-empty keys."""
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or key_field not in reader.fieldnames:
            raise InputSchemaError(f"{path.name} must contain a {key_field!r} column")
        keys: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            key = (row.get(key_field) or "").strip()
            if not key:
                raise InputSchemaError(f"{path.name} row {row_number} has an empty {key_field}")
            if key in keys:
                raise InputSchemaError(f"{path.name} contains duplicate {key_field} {key!r}")
            keys.add(key)
    return keys


def _validate_row(row: dict, product_ids: set[str], location_ids: set[str]) -> tuple[dict | None, str | None]:
    if None in row:
        return None, "row has more values than the header"

    cleaned = {key: (value or "").strip() for key, value in row.items()}
    for field in ("event_id", "event_date", "product_id", "event_type", "quantity"):
        if not cleaned.get(field):
            return None, f"missing required field: {field}"

    try:
        quantity = int(cleaned["quantity"])
    except ValueError:
        return None, "quantity must be a whole number"

    event_type = cleaned["event_type"].lower()
    cleaned["event_type"] = event_type
    if event_type not in {"receipt", "issue", "transfer", "adjustment"}:
        return None, "event_type must be receipt, issue, transfer, or adjustment"

    event_date = cleaned["event_date"]
    try:
        from datetime import date

        parsed_date = date.fromisoformat(event_date)
        if parsed_date.isoformat() != event_date:
            raise ValueError
    except ValueError:
        return None, "event_date must be a valid ISO date (YYYY-MM-DD)"

    if cleaned["product_id"] not in product_ids:
        return None, f"unknown product_id: {cleaned['product_id']}"

    location = cleaned["location_id"]
    source = cleaned["from_location"]
    target = cleaned["to_location"]

    if event_type == "transfer":
        if quantity <= 0:
            return None, "transfer quantity must be greater than zero"
        if location:
            return None, "transfer must leave location_id blank"
        if not source or not target:
            return None, "transfer requires from_location and to_location"
        if source == target:
            return None, "transfer source and destination must differ"
        if source not in location_ids or target not in location_ids:
            return None, "transfer references an unknown location"
    else:
        if not location:
            return None, f"{event_type} requires location_id"
        if location not in location_ids:
            return None, f"unknown location_id: {location}"
        if source or target:
            return None, f"{event_type} must leave from_location and to_location blank"
        if event_type in {"receipt", "issue"} and quantity <= 0:
            return None, f"{event_type} quantity must be greater than zero"
        if event_type == "adjustment" and quantity == 0:
            return None, "adjustment quantity must not be zero"

    cleaned["quantity"] = quantity
    return cleaned, None


def read_and_validate(
    source_path: Path,
    products_path: Path,
    locations_path: Path,
) -> tuple[list[dict], list[dict], int]:
    """Return accepted rows, traceable rejects, and total source row count."""
    product_ids = load_reference_keys(products_path, "product_id")
    location_ids = load_reference_keys(locations_path, "location_id")
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_event_ids: set[str] = set()
    source_file = source_path.name

    with source_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != TRANSACTION_FIELDS:
            raise InputSchemaError(
                f"{source_file} header must be exactly: {','.join(TRANSACTION_FIELDS)}"
            )

        input_rows = 0
        for row_number, row in enumerate(reader, start=2):
            input_rows += 1
            raw_row = {key: value for key, value in row.items() if key is not None}
            event_id = (row.get("event_id") or "").strip()
            reason = None
            if event_id and event_id in seen_event_ids:
                reason = f"duplicate event_id: {event_id}"
            elif event_id:
                seen_event_ids.add(event_id)

            if reason is None:
                normalized, reason = _validate_row(row, product_ids, location_ids)
            else:
                normalized = None

            if reason:
                rejected.append(
                    {
                        "source_file": source_file,
                        "source_row": row_number,
                        "reason": reason,
                        "raw_event": raw_row,
                    }
                )
            else:
                normalized["source_row"] = row_number
                accepted.append(normalized)

    return accepted, rejected, input_rows
