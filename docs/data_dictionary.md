# Data dictionary

All files are UTF-8 CSV with one header row. Dates use ISO format `YYYY-MM-DD`; quantities are integer inventory units. Identifiers are trimmed, case-sensitive text. Event types are trimmed and normalized to lowercase. Transaction and reference headers must match the listed order exactly.

## Input: transactions.csv

| Field | Type | Nullability | Key / relationship | Rule |
|---|---|---|---|---|
| `event_id` | text | Required | Primary key within the complete input snapshot | Immutable unique event-version ID. The first nonblank occurrence reserves the ID, even when invalid. |
| `event_date` | ISO date text | Required | — | Effective business date of the movement. Must be a real calendar date. |
| `product_id` | text | Required | FK to `products.product_id` | Product for the movement. |
| `event_type` | enum text | Required | — | `receipt`, `issue`, `transfer`, or `adjustment`. |
| `quantity` | integer | Required | — | Positive for receipts, issues, and transfers; signed and nonzero for adjustments. |
| `location_id` | text | Conditional | FK to `locations.location_id` | Required for receipts, issues, adjustments; blank for transfers. |
| `from_location` | text | Conditional | FK to `locations.location_id` | Required for transfers; blank otherwise. Must differ from `to_location`. |
| `to_location` | text | Conditional | FK to `locations.location_id` | Required for transfers; blank otherwise. |
| `recorded_at` | ISO timestamp text | Optional for original version; required for a correction | — | If supplied, must include a UTC offset. Used to audit when the version was recorded, not as its stock-effective date. |
| `supersedes_event_id` | text | Optional | Self-FK to an accepted `event_id` | New immutable version replacing an earlier version. Must use a new `event_id`; links cannot branch or cycle. |

Quantity has no numeric ceiling in the assignment; Python integers are used. Only event-specific sign and zero rules constrain quantities.

An event has one product. Product-to-event is one-to-many. Receipts, issues, and adjustments have one location. A transfer has two role-specific location references; the source and destination must be different. Product and location names are descriptive reference columns and are not copied into transaction rows.

### Correction and duplicate policy

A correction preserves the original row and adds a new row with a new ID. The latest valid leaf in each `supersedes_event_id` chain is active; ancestors are accepted but marked superseded. Corrections are replacement versions, not additional stock movements. A corrected transfer replaces both location effects. Missing/rejected parent IDs, cycles, and multiple children of one event are rejected.

The first row with a nonblank event ID reserves it, whether or not its data is otherwise valid. When reusing the same output directory, every previously accepted event ID must remain in the complete snapshot with the same normalized movement fields; change an event only by adding a new version. Repeated identical source payloads use reject code `identical_redelivery`; same-ID rows with a different payload use `conflicting_event_id`. This policy is case-sensitive after trimming.

## Reference inputs

### products.csv

| Field | Type | Nullability | Key | Rule |
|---|---|---|---|---|
| `product_id` | text | Required | Primary key | Unique and nonblank. |
| `product_name` | text | Optional | — | Descriptive name; may be blank and is not used for validation. |

### locations.csv

| Field | Type | Nullability | Key | Rule |
|---|---|---|---|---|
| `location_id` | text | Required | Primary key | Unique and nonblank. |
| `location_name` | text | Optional | — | Descriptive name; may be blank and is not used for validation. |

Headers are exactly `product_id,product_name` and `location_id,location_name`. Names are optional descriptive values; the pipeline validates the ID columns. Duplicate headers, short/extra reference records, blank keys, and repeated keys fail the run.

## Run artifacts

The successful output directory is `data/output/runs/<run_id>/`. A run ID identifies an execution attempt and is intentionally random; it is not a business-event identity.

### accepted_events.csv

| Field | Type | Nullability | Key / relationship | Meaning |
|---|---|---|---|---|
| `run_id` | text | Required | FK to run manifest | Execution that read this version. |
| `source_file` | text | Required | — | Normalized project-relative input path, or resolved path when outside the project. |
| `source_sha256` | hex text | Required | — | Digest of the archived transaction input. |
| `source_record` | integer | Required | Part of source identity | One-based logical CSV data-record number, excluding the header. |
| `line_start`, `line_end` | integer | Required | — | Physical input line range; may span multiple lines for quoted fields. |
| `event_id` | text | Required | Version ID | Event version ID. |
| `event_date` | ISO date text | Required | — | Effective business date. |
| `recorded_at` | ISO timestamp text | Nullable | — | Normalized UTC timestamp or blank for an original event with no recording time. |
| `product_id` | text | Required | FK to product master | Product movement. |
| `event_type` | enum text | Required | — | Supported event type. |
| `quantity` | integer | Required | — | Normalized movement amount. |
| `location_id`, `from_location`, `to_location` | text | Conditional | FK(s) to location master | Location role fields, with blanks per event type. |
| `supersedes_event_id` | text | Nullable | Self-FK to event version | Version replaced by this row, if any. |
| `version_status` | enum text | Required | — | `active` or `superseded`. |

Source identity is the combination of `source_file`, `source_sha256`, and `source_record`; the run ID identifies the archived copy.

### rejected_records.csv

| Field | Type | Nullability | Meaning |
|---|---|---|---|
| `run_id` | text | Required | Run that quarantined the source record. |
| `source_file` | text | Required | Normalized source path. |
| `source_sha256` | hex text | Required | Digest of the archived transaction input. |
| `source_record` | integer | Required | One-based logical data-record ordinal. |
| `line_start`, `line_end` | integer | Required | Physical line range in the source CSV. |
| `reason_code` | text | Required | Stable machine-readable reason. |
| `reason` | text | Required | Human-readable explanation. |
| `raw_event` | JSON text | Required | Parsed source columns, preserving blank and missing values as distinct values. |
| `extra_values` | JSON array text | Required | Values beyond the declared header, if present. |

Short and extra-width records are quarantined. Malformed quoting or invalid text encoding fails the batch because complete source record boundaries/content cannot be trusted.

### daily_balances.csv

The snapshot key is `(balance_date, product_id, location_id)`. Across archived output runs, add `run_id` as the version dimension. All quantities are integers.

| Field | Type | Nullability | Meaning / formula |
|---|---|---|---|
| `run_id` | text | Required | Run that generated the snapshot. |
| `source_file` | text | Required | Transaction source identity. |
| `source_sha256` | hex text | Required | Transaction snapshot digest. |
| `balance_date` | ISO date text | Required | One calendar date in the dense date range. |
| `product_id` | text | Required | Product key. |
| `location_id` | text | Required | Location key. |
| `opening_quantity` | integer | Required | Zero on the first output day; otherwise previous day's close. |
| `receipts` | integer | Required | Sum of accepted active receipt quantities. |
| `issues` | integer | Required | Positive sum of accepted active issue quantities. |
| `transfers_in`, `transfers_out` | integer | Required | Positive transfer quantities for this location. |
| `adjustments` | integer | Required | Signed sum of active adjustment quantities. |
| `net_change` | integer | Required | Receipts − issues + transfers_in − transfers_out + adjustments. |
| `closing_quantity` | integer | Required | Opening quantity + net change. |
| `movement_count` | integer | Required | Number of event-to-location movements; a transfer counts once at each endpoint. |

The output contains each product/location pair touched by an active event for every date from the first to the last active event. No-movement days carry the prior closing balance forward. The maximum supported global date span is 3,660 days and the daily grid is limited to 250,000 rows. Opening stock is zero; negative stock is permitted.

### run_summary.json

Contains the run ID, UTC timestamps, status (`success`, `success_with_rejects`, or `failed`), data-quality status, counts, reject rate, source metadata, environment, elapsed milliseconds, reconciliation checks, and a typed error with stage on failure. `input_rows = accepted_rows + rejected_rows` must pass.

### run_manifest.json and latest_success.json

The run manifest lists input logical paths, archived snapshot paths, SHA-256 digests, byte sizes, artifact checksums, and reconciliation status. `latest_success.json` is atomically replaced after all successful artifacts are ready. It references one run manifest and reports published row counts. Consumers must follow it instead of scanning run directories. The within-run output set is immutable after publication.

## Relationship summary

- Product 1:N event version.
- Location 1:N event location role; transfer records contain two location roles.
- An event version may supersede zero or one earlier version. An earlier version may have at most one accepted successor.
- Run 1:N accepted event versions, rejected source rows, and daily balance rows.
- Within a run, the daily balance key is date + product + location; across runs, run ID distinguishes snapshots.
- Each run manifest 1:N archived source files and generated artifacts.


## Correction update: 31 October 2026

Clean setup: `git clone https://github.com/Yash301201/inventory-balance-pipeline.git`,
then `cd inventory-balance-pipeline` and `python -m unittest discover -s tests -v`.

Each output root is a history continuity boundary. Unrelated histories require a
new output root. A local `.writer.lock` uses atomic exclusive file creation before
prior-history access and remains held through pointer publication and cleanup.
Overlapping writers fail fast; different roots are independent. Normal exceptions
release the lock. After a crash, confirm no writer is running before manually
removing a stale lock. Supported storage is a local filesystem with atomic exclusive
creation; distributed writers/storage are outside this contract.

`artifact_state=prepared` and legacy `publication_status=prepared` describe immutable
pre-commit artifacts. `latest_success.json` is the publication commit record: its
referenced run is active; other prepared runs are inactive (possibly previously
active). Failed attempts have `status=failed` and `publication_status=not_published`.
Activated artifacts are never rewritten.

Successful summary `finished_at` and `elapsed_ms` end after persisted CSV verification;
they exclude summary/manifest writes, hashing, directory promotion and pointer commit.
`timing_scope` states this explicitly. Pointer `published_at` records activation, not
full process duration. Failure timing ends at failure detection. Generation is excluded.

Before hashing/activation, CSV headers, row counts, run IDs and exact serialized
content are checked; daily readback also undergoes granular reconciliation.
Numeric readback accepts signed ASCII integer strings `[+-]?[0-9]+`, integers and
finite integral floats/Decimals. Boolean, fractional, non-finite and malformed values fail.
