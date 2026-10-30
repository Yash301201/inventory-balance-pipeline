# Design note

## Flow

```text
transactions.csv + product/location references
                 ↓
       schema and rule validation
          ↙              ↘
accepted events       rejected rows
          ↓                  ↓
sort and aggregate     rejected_records.csv
          ↓
daily stock balances → daily_balances.csv
          ↓
movement reconciliation + run metrics → run_summary.json
```

## Recovery and backfills

Every run reads the complete source file and rewrites its output CSVs. Re-running the same source therefore does not append duplicate balances. To correct or add a late event, update the source file and rerun; the complete history is recalculated in date and event-ID order. Failed runs write an error and status to `run_summary.json`.

## Trade-offs and limits

- CSV and the Python standard library keep setup simple and make the project easy to run locally.
- The whole input is loaded and sorted in memory. This is appropriate for the requested starter project and 100,000-row exercise; a database or distributed processing engine would suit much larger histories.
- Reference files provide a minimal referential-integrity check. Production systems would normally source these from governed master data.
- No opening-balance import or transaction correction/reversal model is included. The source event history is the record to correct, and balances begin at zero.
- Negative stock is visible in outputs but not rejected because the business brief did not define a negative-stock policy.
