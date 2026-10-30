# Data dictionary

All CSV files are UTF-8 with a header row. Dates use `YYYY-MM-DD`; quantities are whole units. Names use `snake_case`.

## Input: `transactions.csv`

| Field | Required | Meaning / rule |
|---|---|---|
| `event_id` | Yes | Unique event ID. The first row claims an ID; later rows with the same ID are rejected, even if the first row is otherwise invalid. |
| `event_date` | Yes | Date the inventory movement happened. |
| `product_id` | Yes | Must exist in `products.csv`. |
| `event_type` | Yes | `receipt`, `issue`, `transfer`, or `adjustment`. |
| `quantity` | Yes | Integer. Positive for receipts, issues, and transfers; signed nonzero value for adjustments. |
| `location_id` | By type | Required for receipt, issue, and adjustment; blank for transfer. |
| `from_location` | By type | Required for transfer; blank otherwise. Must exist in `locations.csv`. |
| `to_location` | By type | Required for transfer; blank otherwise. Must exist in `locations.csv` and differ from source. |

## Reference inputs

- `products.csv`: unique `product_id` and a human-readable `product_name`.
- `locations.csv`: unique `location_id` and a human-readable `location_name`.

## Output: `daily_balances.csv`

One row per calendar date, product, and location that appears in valid events. Dates between the first and last valid event are included, even when there was no movement that day.

| Field | Meaning |
|---|---|
| `run_id`, `source_file` | Run and source lineage. |
| `balance_date`, `product_id`, `location_id` | Daily output grain. |
| `opening_quantity` | Previous day's closing quantity; starts at zero. |
| `receipts`, `issues` | Positive movement totals by category. |
| `transfers_in`, `transfers_out` | Positive transfer totals for this location. |
| `adjustments` | Signed adjustment total. |
| `net_change` | Receipts − issues + transfers in − transfers out + adjustments. |
| `closing_quantity` | Opening quantity + net change. |
| `movement_count` | Number of event-location movements represented by the row. A transfer counts once at each affected location. |

## Other outputs

- `rejected_records.csv`: run ID, source file, one-based CSV row number (header is row 1), rejection reason, and the raw event as JSON.
- `run_summary.json`: status, run timestamps, row counts, elapsed milliseconds, error text when failed, and aggregate stock reconciliation. Transfers net to zero across all locations.
