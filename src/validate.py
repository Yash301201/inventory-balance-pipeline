"""CSV schema, event validation, and immutable event-version resolution."""

import csv
import re
from datetime import date, datetime, timezone
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
    "recorded_at",
    "supersedes_event_id",
]
REFERENCE_FIELDS = {
    "product_id": ["product_id", "product_name"],
    "location_id": ["location_id", "location_name"],
}
CSV_FIELD_PATTERN = r'(?:"(?:[^"]|"")*"|[^",\r\n]*)'
CSV_RECORD_PATTERN = re.compile(
    CSV_FIELD_PATTERN + r'(?:,' + CSV_FIELD_PATTERN + r')*(?:\r\n|\r|\n|$)',
    re.DOTALL,
)


class InputSchemaError(ValueError):
    """Raised when a source CSV cannot be interpreted using its contract."""


def _validate_csv_quote_syntax(path: Path) -> None:
    """Reject malformed quoting before parsing with the CSV reader."""
    text = path.read_text(encoding="utf-8-sig")
    position = 0
    while position < len(text):
        match = CSV_RECORD_PATTERN.match(text, position)
        if match is None or match.end() == position:
            physical_line = text.count("\n", 0, position) + 1
            raise InputSchemaError(
                f"{path.name} has malformed CSV quoting near physical line {physical_line}"
            )
        position = match.end()


def _unique_header(header: list[str], path: Path) -> None:
    if len(header) != len(set(header)):
        raise InputSchemaError(f"{path.name} contains duplicate column names")


def load_reference_keys(path: Path, key_field: str) -> set[str]:
    """Read and validate the supported product or location reference file."""
    expected = REFERENCE_FIELDS.get(key_field)
    if expected is None:
        raise ValueError(f"unsupported reference key: {key_field}")
    keys: set[str] = set()
    _validate_csv_quote_syntax(path)
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)
            if header is None:
                raise InputSchemaError(f"{path.name} is empty; expected header {','.join(expected)}")
            _unique_header(header, path)
            if header != expected:
                raise InputSchemaError(
                    f"{path.name} header must be exactly: {','.join(expected)}"
                )
            for record_number, row in enumerate(reader, start=1):
                if len(row) != len(header):
                    raise InputSchemaError(
                        f"{path.name} record {record_number} has {len(row)} columns; "
                        f"expected {len(header)}"
                    )
                key = row[0].strip()
                if not key:
                    raise InputSchemaError(
                        f"{path.name} record {record_number} has an empty {key_field}"
                    )
                if key in keys:
                    raise InputSchemaError(f"{path.name} contains duplicate {key_field} {key!r}")
                keys.add(key)
    except csv.Error as error:
        raise InputSchemaError(f"{path.name} has malformed CSV near line {reader.line_num}: {error}") from error
    return keys


def _parse_recorded_at(value: str) -> str:
    """Return an aware timestamp normalized to UTC, or raise ValueError."""
    candidate = value.strip()
    if not candidate:
        return ""
    parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("recorded_at must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_row(
    row: dict[str, str],
    product_ids: set[str],
    location_ids: set[str],
) -> tuple[dict | None, str | None, str | None]:
    """Return normalized event, stable reason code, and readable rejection reason."""
    cleaned = {key: (value or "").strip() for key, value in row.items()}
    for field in ("event_id", "event_date", "product_id", "event_type", "quantity"):
        if not cleaned.get(field):
            return None, "missing_required_field", f"missing required field: {field}"

    try:
        quantity = int(cleaned["quantity"])
    except ValueError:
        return None, "invalid_quantity", "quantity must be a whole number"

    event_type = cleaned["event_type"].lower()
    if event_type not in {"receipt", "issue", "transfer", "adjustment"}:
        return None, "invalid_event_type", "event_type must be receipt, issue, transfer, or adjustment"

    try:
        parsed_date = date.fromisoformat(cleaned["event_date"])
        if parsed_date.isoformat() != cleaned["event_date"]:
            raise ValueError
    except ValueError:
        return None, "invalid_event_date", "event_date must be a valid ISO date (YYYY-MM-DD)"

    try:
        recorded_at = _parse_recorded_at(cleaned["recorded_at"])
    except ValueError as error:
        return None, "invalid_recorded_at", str(error)

    if cleaned["product_id"] not in product_ids:
        return None, "unknown_product", f"unknown product_id: {cleaned['product_id']}"

    location = cleaned["location_id"]
    source = cleaned["from_location"]
    target = cleaned["to_location"]

    if event_type == "transfer":
        if quantity <= 0:
            return None, "invalid_transfer_quantity", "transfer quantity must be greater than zero"
        if location:
            return None, "invalid_transfer_location_fields", "transfer must leave location_id blank"
        if not source or not target:
            return None, "missing_transfer_endpoint", "transfer requires from_location and to_location"
        if source == target:
            return None, "same_location_transfer", "transfer source and destination must differ"
        if source not in location_ids or target not in location_ids:
            return None, "unknown_transfer_location", "transfer references an unknown location"
    else:
        if not location:
            return None, "missing_location", f"{event_type} requires location_id"
        if location not in location_ids:
            return None, "unknown_location", f"unknown location_id: {location}"
        if source or target:
            return None, "invalid_location_fields", f"{event_type} must leave from_location and to_location blank"
        if event_type in {"receipt", "issue"} and quantity <= 0:
            return None, "invalid_movement_quantity", f"{event_type} quantity must be greater than zero"
        if event_type == "adjustment" and quantity == 0:
            return None, "zero_adjustment", "adjustment quantity must not be zero"

    supersedes = cleaned["supersedes_event_id"]
    if supersedes and supersedes == cleaned["event_id"]:
        return None, "self_supersession", "an event version cannot supersede itself"
    if supersedes and not recorded_at:
        return None, "missing_recorded_at", "a corrected version requires recorded_at with a UTC offset"

    cleaned.update(
        {
            "event_type": event_type,
            "quantity": quantity,
            "recorded_at": recorded_at,
            "supersedes_event_id": supersedes,
            "event_date": parsed_date.isoformat(),
        }
    )
    return cleaned, None, None


def _reject(
    source_file: str,
    source_sha256: str,
    source_record: int,
    line_start: int,
    line_end: int,
    reason_code: str,
    reason: str,
    raw_event: dict,
    extra_values: list[str],
) -> dict:
    return {
        "source_file": source_file,
        "source_sha256": source_sha256,
        "source_record": source_record,
        "line_start": line_start,
        "line_end": line_end,
        "reason_code": reason_code,
        "reason": reason,
        "raw_event": raw_event,
        "extra_values": extra_values,
    }


def _reject_event(
    event: dict,
    rejected: list[dict],
    reason_code: str,
    reason: str,
) -> None:
    rejected.append(
        _reject(
            event["source_file"],
            event["source_sha256"],
            event["source_record"],
            event["line_start"],
            event["line_end"],
            reason_code,
            reason,
            event["raw_event"],
            event["extra_values"],
        )
    )


def _resolve_versions(events: list[dict], rejected: list[dict]) -> list[dict]:
    """Reject broken or branching correction links; mark the remaining event versions."""
    by_id = {event["event_id"]: event for event in events}
    rejected_ids: set[str] = set()

    def remove_ids(ids: set[str], code: str, message_for) -> None:
        nonlocal events, by_id, rejected_ids
        for event_id in sorted(ids):
            if event_id in rejected_ids:
                continue
            event = by_id[event_id]
            _reject_event(event, rejected, code, message_for(event))
            rejected_ids.add(event_id)
        events = [event for event in events if event["event_id"] not in rejected_ids]
        by_id = {event["event_id"]: event for event in events}

    # Invalid references can cascade, so repeat until the chain is stable.
    while True:
        missing_parent_ids = {
            event["event_id"]
            for event in events
            if event["supersedes_event_id"] and event["supersedes_event_id"] not in by_id
        }
        if missing_parent_ids:
            remove_ids(
                missing_parent_ids,
                "unknown_superseded_event",
                lambda event: (
                    f"superseded event {event['supersedes_event_id']!r} is missing or rejected"
                ),
            )
            continue

        children: dict[str, list[str]] = {}
        for event in events:
            if event["supersedes_event_id"]:
                children.setdefault(event["supersedes_event_id"], []).append(event["event_id"])
        branch_ids = {
            child_id
            for child_ids in children.values()
            if len(child_ids) > 1
            for child_id in child_ids
        }
        if branch_ids:
            remove_ids(
                branch_ids,
                "conflicting_supersession",
                lambda event: (
                    f"multiple versions supersede event {event['supersedes_event_id']!r}; "
                    "all conflicting versions were rejected"
                ),
            )
            continue

        cycle_ids: set[str] = set()
        finished: set[str] = set()
        for start_id in by_id:
            path: list[str] = []
            seen_at: dict[str, int] = {}
            current_id = start_id
            while (
                current_id in by_id
                and current_id not in finished
                and by_id[current_id]["supersedes_event_id"]
            ):
                if current_id in seen_at:
                    cycle_ids.update(path[seen_at[current_id]:])
                    break
                seen_at[current_id] = len(path)
                path.append(current_id)
                current_id = by_id[current_id]["supersedes_event_id"]
            finished.update(path)
        if cycle_ids:
            remove_ids(
                cycle_ids,
                "supersession_cycle",
                lambda event: "event correction links form a cycle",
            )
            continue

        bad_timestamp_ids: set[str] = set()
        for event in events:
            parent_id = event["supersedes_event_id"]
            if not parent_id:
                continue
            parent = by_id[parent_id]
            if parent["recorded_at"] and event["recorded_at"] <= parent["recorded_at"]:
                bad_timestamp_ids.add(event["event_id"])
        if bad_timestamp_ids:
            remove_ids(
                bad_timestamp_ids,
                "invalid_correction_time",
                lambda event: (
                    f"recorded_at must be later than superseded event "
                    f"{event['supersedes_event_id']!r}"
                ),
            )
            continue
        break

    superseded_ids = {event["supersedes_event_id"] for event in events if event["supersedes_event_id"]}
    for event in events:
        event["version_status"] = "superseded" if event["event_id"] in superseded_ids else "active"
    return events


def read_and_validate(
    source_path: Path,
    products_path: Path,
    locations_path: Path,
    *,
    source_file: str | None = None,
    source_sha256: str = "",
    progress: dict | None = None,
) -> tuple[list[dict], list[dict], int]:
    """Return valid event versions, rejected records, and logical source-record count."""
    product_ids = load_reference_keys(products_path, "product_id")
    location_ids = load_reference_keys(locations_path, "location_id")
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_event_ids: dict[str, tuple[str, ...]] = {}
    logical_source = source_file or source_path.resolve().as_posix()

    def update_progress(input_rows: int) -> None:
        if progress is not None:
            progress["input_rows"] = input_rows
            progress["accepted_rows"] = len(accepted)
            progress["rejected_rows"] = len(rejected)

    try:
        if source_path.stat().st_size == 0:
            raise InputSchemaError(
                f"{logical_source} is zero bytes; include the required CSV header"
            )
        _validate_csv_quote_syntax(source_path)
        with source_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)
            if header is None:
                raise InputSchemaError(
                    f"{logical_source} has no header; zero-byte input is not a header-only file"
                )
            _unique_header(header, source_path)
            if header != TRANSACTION_FIELDS:
                raise InputSchemaError(
                    f"{logical_source} header must be exactly: {','.join(TRANSACTION_FIELDS)}"
                )

            input_rows = 0
            previous_physical_line = reader.line_num
            for input_rows, values in enumerate(reader, start=1):
                line_start = previous_physical_line + 1
                line_end = reader.line_num
                previous_physical_line = line_end

                raw_event = {
                    field: values[index] if index < len(values) else None
                    for index, field in enumerate(header)
                }
                extra_values = values[len(header):] if len(values) > len(header) else []
                event_id = (values[0] if values else "").strip()
                raw_signature = tuple(value.strip() for value in values)
                if event_id and event_id in seen_event_ids:
                    duplicate_code = (
                        "identical_redelivery"
                        if seen_event_ids[event_id] == raw_signature
                        else "conflicting_event_id"
                    )
                    reason = (
                        f"duplicate event_id {event_id!r}: "
                        + (
                            "identical redelivery"
                            if duplicate_code == "identical_redelivery"
                            else "different payload conflicts with the first row"
                        )
                    )
                    rejected.append(
                        _reject(
                            logical_source, source_sha256, input_rows,
                            line_start, line_end, duplicate_code, reason,
                            raw_event, extra_values,
                        )
                    )
                    update_progress(input_rows)
                    continue
                if event_id:
                    # The first nonblank ID is reserved, even when its row is invalid.
                    seen_event_ids[event_id] = raw_signature

                if len(values) != len(header):
                    code = "short_record" if len(values) < len(header) else "extra_values"
                    reason = (
                        f"record has {len(values)} columns; expected {len(header)}"
                    )
                    rejected.append(
                        _reject(
                            logical_source, source_sha256, input_rows,
                            line_start, line_end, code, reason,
                            raw_event, extra_values,
                        )
                    )
                    update_progress(input_rows)
                    continue

                normalized, reason_code, reason = _validate_row(
                    dict(zip(header, values)), product_ids, location_ids
                )
                if reason:
                    rejected.append(
                        _reject(
                            logical_source, source_sha256, input_rows,
                            line_start, line_end, reason_code or "invalid_record",
                            reason, raw_event, extra_values,
                        )
                    )
                else:
                    normalized.update(
                        {
                            "source_file": logical_source,
                            "source_sha256": source_sha256,
                            "source_record": input_rows,
                            "line_start": line_start,
                            "line_end": line_end,
                            "raw_event": raw_event,
                            "extra_values": extra_values,
                        }
                    )
                    accepted.append(normalized)
                update_progress(input_rows)
    except csv.Error as error:
        raise InputSchemaError(
            f"{logical_source} has malformed CSV near physical line "
            f"{reader.line_num}: {error}; the batch was not processed"
        ) from error

    accepted = _resolve_versions(accepted, rejected)
    if progress is not None:
        progress["input_rows"] = input_rows
        progress["accepted_rows"] = len(accepted)
        progress["rejected_rows"] = len(rejected)
    return accepted, rejected, input_rows




def validate_event_history(previous_versions: list[dict], current_versions: list[dict]) -> None:
    """Enforce immutable IDs and complete history against the prior successful snapshot."""
    if not previous_versions:
        return
    current_by_id = {event["event_id"]: event for event in current_versions}
    previous_by_id = {event["event_id"]: event for event in previous_versions}
    missing_ids = sorted(set(previous_by_id) - set(current_by_id))
    if missing_ids:
        preview = ", ".join(missing_ids[:5])
        suffix = "..." if len(missing_ids) > 5 else ""
        raise InputSchemaError(
            f"complete historical snapshot is missing prior accepted event IDs: {preview}{suffix}"
        )

    immutable_fields = (
        "event_date",
        "recorded_at",
        "product_id",
        "event_type",
        "quantity",
        "location_id",
        "from_location",
        "to_location",
        "supersedes_event_id",
    )
    changed_ids = []
    for event_id, previous in previous_by_id.items():
        current = current_by_id[event_id]
        if any(str(previous.get(field, "")) != str(current.get(field, "")) for field in immutable_fields):
            changed_ids.append(event_id)
    if changed_ids:
        preview = ", ".join(sorted(changed_ids)[:5])
        suffix = "..." if len(changed_ids) > 5 else ""
        raise InputSchemaError(
            f"event IDs are immutable; changed prior versions: {preview}{suffix}. "
            "Add a new event ID and supersedes_event_id for a correction."
        )
