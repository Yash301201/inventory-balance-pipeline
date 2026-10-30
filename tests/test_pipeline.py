import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from transform import compute_daily_balances, reconcile
import main as pipeline_main
from main import run_pipeline
from validate import InputSchemaError, TRANSACTION_FIELDS, read_and_validate


class PipelineTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.products = self.root / "products.csv"
        self.locations = self.root / "locations.csv"
        self.input = self.root / "transactions.csv"
        self.products.write_text("product_id,product_name\nP1,Product 1\nP2,Product 2\n", encoding="utf-8")
        self.locations.write_text("location_id,location_name\nA,Location A\nB,Location B\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write_events(self, rows):
        with self.input.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def event(event_id, event_date, product_id="P1", event_type="receipt", quantity=5,
              location_id="A", from_location="", to_location=""):
        return {
            "event_id": event_id,
            "event_date": event_date,
            "product_id": product_id,
            "event_type": event_type,
            "quantity": quantity,
            "location_id": location_id,
            "from_location": from_location,
            "to_location": to_location,
        }

    def test_happy_path_calculates_daily_balances_and_reconciles(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=10),
            self.event("E2", "2026-01-02", event_type="issue", quantity=3),
        ])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        rows = compute_daily_balances(events, "run-1", self.input.name)
        self.assertEqual((count, len(events), len(rejects)), (2, 2, 0))
        self.assertEqual([row["closing_quantity"] for row in rows], [10, 7])
        self.assertTrue(reconcile(events, rows)["passed"])

    def test_empty_input_is_valid(self):
        self.write_events([])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual((events, rejects, count), ([], [], 0))
        self.assertEqual(compute_daily_balances(events, "run-1", self.input.name), [])

    def test_malformed_date_is_rejected(self):
        self.write_events([self.event("E1", "not-a-date")])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual((len(events), len(rejects), count), (0, 1, 1))
        self.assertIn("ISO date", rejects[0]["reason"])

    def test_duplicate_event_id_is_rejected(self):
        row = self.event("E1", "2026-01-01")
        self.write_events([row, row])
        events, rejects, _ = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual(len(events), 1)
        self.assertIn("duplicate event_id", rejects[0]["reason"])

    def test_missing_required_field_is_rejected(self):
        row = self.event("E1", "2026-01-01")
        row["product_id"] = ""
        self.write_events([row])
        events, rejects, _ = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual(events, [])
        self.assertIn("missing required field: product_id", rejects[0]["reason"])

    def test_unexpected_header_fails_clearly(self):
        self.input.write_text("event_id,wrong_column\n", encoding="utf-8")
        with self.assertRaises(InputSchemaError):
            read_and_validate(self.input, self.products, self.locations)

    def test_transfer_moves_stock_without_changing_company_total(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=10),
            self.event("E2", "2026-01-02", event_type="transfer", quantity=4,
                       location_id="", from_location="A", to_location="B"),
        ])
        events, _, _ = read_and_validate(self.input, self.products, self.locations)
        rows = compute_daily_balances(events, "run-1", self.input.name)
        latest = {(r["product_id"], r["location_id"]): r for r in rows}
        self.assertEqual(latest[("P1", "A")]["closing_quantity"], 6)
        self.assertEqual(latest[("P1", "B")]["closing_quantity"], 4)
        self.assertTrue(reconcile(events, rows)["passed"])

    def test_backdated_receipt_recomputes_later_balances(self):
        self.write_events([
            self.event("E1", "2026-01-02", event_type="issue", quantity=3),
            self.event("E2", "2026-01-01", quantity=10),
        ])
        events, _, _ = read_and_validate(self.input, self.products, self.locations)
        rows = compute_daily_balances(events, "run-1", self.input.name)
        self.assertEqual([r["balance_date"] for r in rows], ["2026-01-01", "2026-01-02"])
        self.assertEqual([r["closing_quantity"] for r in rows], [10, 7])

    def test_rerunning_overwrites_outputs_without_adding_duplicate_rows(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=10),
            self.event("E2", "2026-01-02", event_type="issue", quantity=3),
        ])
        output = self.root / "output"
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 0)
        with (output / "daily_balances.csv").open(encoding="utf-8") as handle:
            first = list(csv.DictReader(handle))
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 0)
        with (output / "daily_balances.csv").open(encoding="utf-8") as handle:
            second = list(csv.DictReader(handle))
        self.assertEqual(len(first), len(second))
        self.assertEqual([r["closing_quantity"] for r in first], [r["closing_quantity"] for r in second])

    def test_partial_output_failure_is_reported_and_retry_succeeds(self):
        self.write_events([self.event("E1", "2026-01-01", quantity=5)])
        output = self.root / "output"
        original_write_csv = pipeline_main.write_csv

        def fail_on_reject_file(path, fields, rows):
            if path.name == "rejected_records.csv":
                raise OSError("simulated output write failure")
            original_write_csv(path, fields, rows)

        with patch("main.write_csv", side_effect=fail_on_reject_file):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 1)

        failed_summary = json.loads((output / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(failed_summary["status"], "failed")
        self.assertIn("simulated output write failure", failed_summary["error"])
        self.assertTrue((output / "daily_balances.csv").exists())

        self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 0)
        recovered_summary = json.loads((output / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(recovered_summary["status"], "success")
        with (output / "daily_balances.csv").open(encoding="utf-8") as handle:
            rows_after_retry = list(csv.DictReader(handle))
        self.assertEqual(len(rows_after_retry), 1)
        self.assertTrue((output / "rejected_records.csv").exists())


if __name__ == "__main__":
    unittest.main()
