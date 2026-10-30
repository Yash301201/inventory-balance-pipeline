import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from transform import compute_daily_balances, reconcile
from validate import TRANSACTION_FIELDS, read_and_validate


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.products = self.root / "products.csv"
        self.locations = self.root / "locations.csv"
        self.input = self.root / "transactions.csv"
        self.products.write_text("product_id,product_name\nP1,Product 1\n", encoding="utf-8")
        self.locations.write_text("location_id,location_name\nA,Location A\nB,Location B\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def event(event_id, event_date, event_type, quantity, location="A", source="", target=""):
        return {
            "event_id": event_id,
            "event_date": event_date,
            "product_id": "P1",
            "event_type": event_type,
            "quantity": quantity,
            "location_id": location,
            "from_location": source,
            "to_location": target,
            "recorded_at": "",
            "supersedes_event_id": "",
        }

    def compute(self, rows):
        with self.input.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        events, rejected, count = read_and_validate(self.input, self.products, self.locations)
        balances = compute_daily_balances(events, "run", "transactions.csv")
        return events, balances, reconcile(
            events,
            balances,
            input_rows=count,
            accepted_rows=len(events),
            rejected_rows=len(rejected),
        )

    def test_shifted_movement_category_and_missing_date_fail(self):
        events, balances, good = self.compute([
            self.event("E1", "2026-01-01", "receipt", 10),
            self.event("E2", "2026-01-03", "issue", 2),
        ])
        self.assertTrue(good["passed"])
        shifted = [row.copy() for row in balances]
        shifted[0]["receipts"] += 1
        result = reconcile(events, shifted)
        self.assertFalse(result["checks"]["movement_categories_match_source"])
        missing_day = [row for row in balances if row["balance_date"] != "2026-01-02"]
        self.assertFalse(reconcile(events, missing_day)["checks"]["expected_date_product_location_coverage"])

    def test_missing_transfer_side_and_changed_transfer_quantity_fail(self):
        events, balances, _ = self.compute([
            self.event("E1", "2026-01-01", "receipt", 10),
            self.event("E2", "2026-01-02", "transfer", 4, location="", source="A", target="B"),
        ])
        changed = [row.copy() for row in balances]
        b_row = next(row for row in changed if row["balance_date"] == "2026-01-02" and row["location_id"] == "B")
        b_row["transfers_in"] += 1
        result = reconcile(events, changed)
        self.assertFalse(result["checks"]["movement_categories_match_source"])
        self.assertFalse(result["checks"]["transfer_quantities_balance_by_product_date"])

    def test_count_reconciliation_fails_when_a_source_record_is_unaccounted_for(self):
        events, balances, _ = self.compute([self.event("E1", "2026-01-01", "receipt", 10)])
        result = reconcile(events, balances, input_rows=2, accepted_rows=1, rejected_rows=0)
        self.assertFalse(result["checks"]["count_reconciliation_passed"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
