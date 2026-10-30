# Design and operational behavior

## Pipeline flow

The pipeline archives the exact transaction and master-file bytes, then validates the archived copies. Validation either produces accepted event versions plus quarantined records, or fails the batch when the CSV cannot be parsed safely. Accepted correction chains are resolved before movement aggregation. Active versions feed the daily balance builder; all accepted versions remain available for audit and source-to-output lineage.

All outputs are written into a private staging directory for one run. The summary and a hash manifest are completed before the run directory is renamed into place. The final operation atomically replaces `latest_success.json`; readers follow that pointer to obtain one coherent set. A failed stage is saved in a separate `failed-<run_id>` directory when possible. The pointer to the previous successful run is left unchanged.

## Correction and history model

The input is a complete historical snapshot, not an incremental batch. Event IDs identify immutable versions. A corrected version has a new `event_id`, a `supersedes_event_id` linking it to the version it replaces, and a timezone-aware `recorded_at`. The business `event_date` remains the effective date used for historical balances. When reusing the same output directory, prior accepted event IDs must remain present and unchanged; an omitted or edited prior version fails validation. The valid leaf of each correction chain is active; earlier versions are retained as superseded. Branches and cycles are rejected. A corrected transfer is one replacement event, so its two location effects change together.

Each run archives its three source files and records the source path, SHA-256 digest, byte size, and snapshot path in the manifest. `accepted_events.csv` preserves normalized event values and their status. Rejected records include the source digest, logical record number, physical line range, stable reason code, raw parsed fields, and any extra columns.

## Reconciliation

Reconciliation independently rebuilds expected movement categories from active source events and checks:

1. unique `(balance_date, product_id, location_id)` output keys;
2. complete dense date/product/location coverage;
3. receipts, issues, transfers in/out, adjustments, and movement counts at each key;
4. opening balance continuity and `closing = opening + net_change`;
5. transfer-in and transfer-out quantity equality by product and date;
6. company ending inventory against receipts minus issues plus adjustments; and
7. `input_rows = accepted_rows + rejected_rows`.

Any failed check keeps the run out of `latest_success.json`.

## Failure handling and run results

`run_summary.json` records stage counts, data-quality status, reject rate, reconciliation checks, elapsed time, Python version, platform, and a typed error with the failing stage when a run fails. A batch with valid and rejected rows returns exit code zero with `success_with_rejects`; malformed CSV syntax, unreadable input, failed reconciliation, or output errors return a nonzero exit code. If the destination cannot be written, a durable failure summary may not be possible; the exception is also logged to stderr.

The summary's `calculated_balance_rows` and `prepared_balance_rows` are distinct from the counts in `latest_success.json`'s `published_row_counts`. The latest-success pointer is the publication commit marker. Consumers must not infer a successful publication by scanning run directories.

## Data and scaling choices

- CSV and the Python standard library keep setup simple.
- Product and location IDs are case-sensitive, trimmed text. Master IDs must be unique and nonblank. Master headers are exact.
- Quantity fields are whole inventory units. Receipts, issues, and transfers must be positive; adjustments must be nonzero and signed.
- Opening inventory is zero. Negative balances are shown rather than rejected because the assignment did not specify that policy.
- Dense output spans at most 3,660 calendar days and 250,000 total rows. Longer ranges or larger grids fail clearly before expanding the output.
- The full event history is loaded in memory and sorted. Time includes `O(n log n)` sorting plus dense output work proportional to product/location pairs times calendar days. A database or streaming design would be a future choice for much larger volumes.
- The pipeline is single-process and local. Credentials, cloud services, and infrastructure are not needed for this assignment.
