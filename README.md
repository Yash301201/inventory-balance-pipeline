# Inventory Balance Pipeline

A small, local data engineering project that turns inventory receipts, issues, transfers, and adjustments into daily stock balances.

## Requirements

- Python 3.10 or newer
- No third-party Python packages

## Run it

Open a terminal in this project folder and run:

```powershell
python src/main.py
```

The sample run reads `data/input/transactions.csv` and the product/location reference files. It writes these files to `data/output/`:

- `daily_balances.csv` — one row per date, product, and location
- `rejected_records.csv` — invalid input rows with a reason
- `run_summary.json` — run ID, status, counts, timing, and reconciliation result

The sample intentionally contains a duplicate event, an unknown product, and a missing required value. Those rows should appear in `rejected_records.csv`.

Use different files or an output folder with command-line options:

```powershell
python src/main.py --input data/input/transactions.csv --output-dir data/output
```

Generate a larger reproducible dataset (100,000 rows by default):

```powershell
python src/generate_sample.py --rows 100000 --seed 42 --output data/input/large_transactions.csv
python src/main.py --input data/input/large_transactions.csv --output-dir data/output/large_run
```

Run automated checks:

```powershell
python -m unittest discover -s tests -v
```

## Business rules and assumptions

- Each nonblank `event_id` is claimed by its first row. Later rows with that ID are rejected. If the first row is invalid, it is rejected too and later repeats remain duplicates.
- Receipt and issue quantities are positive whole numbers. Receipts add stock; issues subtract stock.
- Adjustment quantities are signed whole numbers: positive adds stock, negative removes stock.
- A transfer has a positive quantity and moves stock from one known location to a different known location.
- A transaction refers to a product and location present in the reference CSV files.
- There is no opening stock in this starter project. Balances begin at zero, then transactions change them.
- The pipeline recomputes the full history from the input file on every run. This makes reruns safe and includes late or backdated events in the recalculated balances.
- Negative balances are allowed and shown; the brief does not define whether they should be rejected.
- A malformed or changed transaction header fails the run clearly instead of silently interpreting columns incorrectly.

## Design and performance

The pipeline validates once, sorts accepted events by date and ID, aggregates their daily movements, then calculates running balances. Sorting is `O(n log n)` and the balance calculation is linear in the number of daily product-location rows. For much larger volumes, a database engine and incremental backfill strategy would be reasonable next steps.

Each run records elapsed milliseconds in `run_summary.json`. To measure the larger case, run the 100,000-row commands above and read the `elapsed_ms` value from the resulting summary. The measurement is machine-dependent.

Example measurement from this project run: 100,000 events were accepted, 1,460 daily balance rows were produced, reconciliation difference was zero, and processing took about 791 ms in the current environment. See `data/output/large_run/run_summary.json` for the recorded run details.

See [`docs/data_dictionary.md`](docs/data_dictionary.md) for the input/output fields and [`docs/design.md`](docs/design.md) for flow, operational behavior, and trade-offs.
