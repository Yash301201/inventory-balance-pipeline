import csv
import hashlib
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
from validate import TRANSACTION_FIELDS, read_and_validate


class PipelineTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.products = self.root / "products.csv"
        self.locations = self.root / "locations.csv"
        self.input = self.root / "transactions.csv"
        self.products.write_text(
            "product_id,product_name\nP1,Product 1\nP2,Product 2\n", encoding="utf-8"
        )
        self.locations.write_text(
            "location_id,location_name\nA,Location A\nB,Location B\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_events(self, rows):
        with self.input.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def event(event_id, event_date, product_id="P1", event_type="receipt", quantity=5,
              location_id="A", from_location="", to_location="", recorded_at="",
              supersedes_event_id=""):
        return {
            "event_id": event_id,
            "event_date": event_date,
            "product_id": product_id,
            "event_type": event_type,
            "quantity": quantity,
            "location_id": location_id,
            "from_location": from_location,
            "to_location": to_location,
            "recorded_at": recorded_at,
            "supersedes_event_id": supersedes_event_id,
        }

    def latest(self, output):
        return json.loads((output / "latest_success.json").read_text(encoding="utf-8"))

    def run_dir(self, output, pointer=None):
        pointer = pointer or self.latest(output)
        return output / Path(pointer["manifest"]).parent

    def read_rows(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_happy_path_calculates_daily_balances_and_reconciles(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=10),
            self.event("E2", "2026-01-02", event_type="issue", quantity=3),
        ])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        rows = compute_daily_balances(events, "run-1", self.input.name)
        result = reconcile(events, rows, input_rows=count, accepted_rows=len(events), rejected_rows=len(rejects))
        self.assertEqual((count, len(events), len(rejects)), (2, 2, 0))
        self.assertEqual([row["closing_quantity"] for row in rows], [10, 7])
        self.assertTrue(result["passed"])

    def test_empty_header_only_input_is_valid(self):
        self.write_events([])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual((events, rejects, count), ([], [], 0))
        self.assertEqual(compute_daily_balances(events, "run-1", self.input.name), [])
        self.assertTrue(reconcile(events, [])["passed"])

    def test_malformed_date_is_rejected(self):
        self.write_events([self.event("E1", "not-a-date")])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual((len(events), len(rejects), count), (0, 1, 1))
        self.assertEqual(rejects[0]["reason_code"], "invalid_event_date")

    def test_identical_and_conflicting_duplicate_event_ids_have_distinct_reasons(self):
        first = self.event("E1", "2026-01-01")
        self.write_events([first, first.copy(), self.event("E1", "2026-01-02", quantity=8)])
        events, rejects, _ = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            [row["reason_code"] for row in rejects],
            ["identical_redelivery", "conflicting_event_id"],
        )

    def test_invalid_first_row_reserves_event_id(self):
        invalid = self.event("E1", "bad-date")
        valid = self.event("E1", "2026-01-01")
        self.write_events([invalid, valid])
        events, rejects, _ = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual(events, [])
        self.assertEqual([row["reason_code"] for row in rejects], ["invalid_event_date", "conflicting_event_id"])

    def test_transfer_moves_stock_without_changing_company_total(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=10),
            self.event("E2", "2026-01-02", event_type="transfer", quantity=4,
                       location_id="", from_location="A", to_location="B"),
        ])
        events, _, _ = read_and_validate(self.input, self.products, self.locations)
        rows = compute_daily_balances(events, "run-1", self.input.name)
        latest = {(r["product_id"], r["location_id"]): r for r in rows if r["balance_date"] == "2026-01-02"}
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
        self.assertEqual([row["balance_date"] for row in rows], ["2026-01-01", "2026-01-02"])
        self.assertEqual([row["closing_quantity"] for row in rows], [10, 7])

    def test_reconciliation_detects_offsetting_location_errors_and_duplicate_keys(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=10),
            self.event("E2", "2026-01-02", event_type="transfer", quantity=4,
                       location_id="", from_location="A", to_location="B"),
        ])
        events, _, _ = read_and_validate(self.input, self.products, self.locations)
        rows = compute_daily_balances(events, "run-1", self.input.name)
        latest_rows = [row for row in rows if row["balance_date"] == "2026-01-02"]
        by_location = {row["location_id"]: row for row in latest_rows}
        by_location["A"]["closing_quantity"] += 100
        by_location["B"]["closing_quantity"] -= 100
        result = reconcile(events, rows)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["opening_continuity_and_balance_equation"])
        rows.append(rows[-1].copy())
        self.assertFalse(reconcile(events, rows)["checks"]["unique_balance_keys"])

    def test_reruns_preserve_each_run_and_latest_pointer_selects_current(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=10),
            self.event("E2", "2026-01-02", event_type="issue", quantity=3),
        ])
        output = self.root / "output"
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 0)
        first_pointer = self.latest(output)
        first_dir = self.run_dir(output, first_pointer)
        first_rows = self.read_rows(first_dir / "daily_balances.csv")

        self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 0)
        second_pointer = self.latest(output)
        second_dir = self.run_dir(output, second_pointer)
        second_rows = self.read_rows(second_dir / "daily_balances.csv")
        self.assertNotEqual(first_pointer["run_id"], second_pointer["run_id"])
        self.assertTrue(first_dir.exists())
        self.assertEqual(
            [r["closing_quantity"] for r in first_rows],
            [r["closing_quantity"] for r in second_rows],
        )
        self.assertEqual(second_pointer["published_row_counts"]["daily_balances"], 2)

    def test_manifest_hashes_every_run_artifact_and_archives_inputs(self):
        self.write_events([self.event("E1", "2026-01-01", quantity=5)])
        output = self.root / "output"
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 0)
        run_dir = self.run_dir(output)
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["input_mode"], "complete_historical_snapshot")
        self.assertEqual(manifest["source_files"]["transactions"]["record_count"], 1)
        self.assertEqual(
            hashlib.sha256((run_dir / "input_snapshot/transactions.csv").read_bytes()).hexdigest(),
            manifest["source_files"]["transactions"]["sha256"],
        )
        for name, metadata in manifest["artifacts"].items():
            self.assertEqual(
                hashlib.sha256((run_dir / name).read_bytes()).hexdigest(),
                metadata["sha256"],
            )
        summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["publication_status"], "prepared")
        self.assertEqual(summary["count_reconciliation_passed"], True)

    def test_summary_reports_success_with_rejects_and_exit_code_zero(self):
        self.write_events([
            self.event("E1", "2026-01-01", quantity=5),
            self.event("E2", "invalid", quantity=5),
        ])
        output = self.root / "output"
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, output), 0)
        summary = json.loads((self.run_dir(output) / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "success_with_rejects")
        self.assertEqual(summary["data_quality_status"], "has_rejects")
        self.assertEqual(summary["reject_rate"], 0.5)
        self.assertEqual(summary["input_rows"], summary["accepted_rows"] + summary["rejected_rows"])


if __name__ == "__main__":
    unittest.main()
