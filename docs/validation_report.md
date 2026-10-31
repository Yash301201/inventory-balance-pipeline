# Validation report

## Current project checks

The updated project was verified with:

```sh
python -m unittest discover -s tests -v
```

**Result:** 38 tests passed. Coverage includes correction history and transfer corrections, strict CSV parsing, reference data, duplicate policies, source hashes and lineage, detailed reconciliation, failure recovery, and deterministic generation.

The sample run was also executed with `python src/main.py`:

- 9 input rows, 6 accepted event versions, and 3 rejected rows
- 12 daily balance rows
- Expected ending stock: 12; actual ending stock: 12; difference: 0
- All seven reconciliation checks passed

The separate correction/anomaly fixture contains 11 rows, 5 accepted versions, 6 rejects, and 6 daily balance rows. Its disposition labels and hand-calculated expected balances matched the pipeline output.

## Reproducibility and performance

Two 100,000-row inputs generated with seed 42 had the same SHA-256 checksum:

`a1373b6aeebdf3a05b8ff95ba061d5dbf0bddb593ee028ed9bd9eed05e57ceba`

A 100,000-row pipeline run on Windows 11 with Python 3.13.15 accepted all records, rejected none, produced 1,460 daily rows, and reconciled expected and actual ending inventory to 106,068. Pipeline elapsed time was 9,020.83 ms; data generation time is excluded.

For historical context, the earlier project run recorded about 791 ms with incomplete environment metadata. The independent review measured 723.65 ms on Linux with Python 3.12.14. These earlier runs used the simpler output flow and their environment-specific measurements are not directly comparable to this version, which also archives inputs, validates correction history, hashes artifacts, and writes normalized accepted events.

To repeat the large run:

```sh
python src/generate_sample.py --rows 100000 --seed 42 --output work/large_transactions.csv
python src/main.py --input work/large_transactions.csv --output-dir work/large-output
```

Follow `work/large-output/latest_success.json` to locate the run summary and detailed reconciliation report.

## Output inspection

After `python src/main.py`, read `data/output/latest_success.json`, then open the run directory named by its `manifest` field. Check `run_summary.json`, `run_manifest.json`, `daily_balances.csv`, `accepted_events.csv`, `rejected_records.csv`, and the archived inputs in `input_snapshot/`.


## Local correction verification — 31 October 2026 (India)

Base reviewed commit: `4bd51df5e1ab9f993b003f3874f68e73b2756335`; evidence applies to uncommitted corrections on this base.

Environment: Windows-11-10.0.26300-SP0, Python 3.13.15, standard library only.

Automated suite: 42 tests passed (38 retained plus four regression methods).

Earlier Linux Python 3.12.14 evidence (38 tests) and historical Windows measurements above are separate version/environment observations, not direct performance comparisons.

sample: counts={'accepted_event_versions': 6, 'active_events': 6, 'daily_balance_rows': 12, 'input_rows': 9, 'rejected_rows': 3, 'superseded_event_versions': 0}; reconciliation=True; ending stock=12; time-to-preparation=243.46 ms.

scenario-output: counts={'accepted_event_versions': 5, 'active_events': 4, 'daily_balance_rows': 6, 'input_rows': 11, 'rejected_rows': 6, 'superseded_event_versions': 1}; reconciliation=True; ending stock=5; time-to-preparation=235.27 ms.

large-output: counts={'accepted_event_versions': 100000, 'active_events': 100000, 'daily_balance_rows': 1460, 'input_rows': 100000, 'rejected_rows': 0, 'superseded_event_versions': 0}; reconciliation=True; ending stock=106068; time-to-preparation=3969.46 ms.

Two independently generated 100,000-row seed-42 inputs match SHA-256 `a1373b6aeebdf3a05b8ff95ba061d5dbf0bddb593ee028ed9bd9eed05e57ceba`.

Compare business outputs after excluding varying run IDs/timestamps. Generation is excluded from summary timing; summary/manifest hashing, promotion and pointer commit are also excluded.

Publication uses an atomic local single-writer lock and latest_success.json as commit record. See README correction update for stale-lock recovery, prepared versus active semantics, numeric syntax and unrelated-history output-root requirements.
