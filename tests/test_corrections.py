import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from main import run_pipeline
from transform import compute_daily_balances, reconcile
from validate import TRANSACTION_FIELDS, read_and_validate


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.products = self.root / "products.csv"
        self.locations = self.root / "locations.csv"
        self.input = self.root / "transactions.csv"
        self.output = self.root / "output"
        self.products.write_text("product_id,product_name\nP1,Product 1\n", encoding="utf-8")
        self.locations.write_text("location_id,location_name\nA,Location A\nB,Location B\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def event(event_id, event_date, event_type, quantity, location_id="A",
              from_location="", to_location="", recorded_at="", supersedes_event_id=""):
        return {
            "event_id": event_id,
            "event_date": event_date,
            "product_id": "P1",
            "event_type": event_type,
            "quantity": quantity,
            "location_id": location_id,
            "from_location": from_location,
            "to_location": to_location,
            "recorded_at": recorded_at,
            "supersedes_event_id": supersedes_event_id,
        }

    def write_events(self, rows):
        with self.input.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def read_run_rows(self):
        pointer = json.loads((self.output / "latest_success.json").read_text(encoding="utf-8"))
        run_dir = self.output / Path(pointer["manifest"]).parent
        with (run_dir / "daily_balances.csv").open(newline="", encoding="utf-8") as handle:
            balances = list(csv.DictReader(handle))
        return pointer, run_dir, balances

    def test_fractional_second_and_offset_corrections(self):
        cases = [
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.100000Z", True),
            ("2026-01-01T00:00:00.900000Z", "2026-01-01T00:00:00Z", False),
            ("2026-01-01T00:00:00Z", "2026-01-01T05:30:00+05:30", False),
        ]
        for parent, child, accepted in cases:
            with self.subTest(parent=parent, child=child):
                self.write_events([
                    self.event("E1", "2026-01-01", "receipt", 10, recorded_at=parent),
                    self.event("E2", "2026-01-01", "receipt", 8, recorded_at=child, supersedes_event_id="E1"),
                ])
                events, rejects, _ = read_and_validate(self.input, self.products, self.locations)
                self.assertEqual(len(rejects), 0 if accepted else 1)
                active = [e for e in events if e["version_status"] == "active"]
                self.assertEqual(active[0]["quantity"], 8 if accepted else 10)
                if accepted:
                    self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
                    first = self.read_run_rows()[2]
                    self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
                    second = self.read_run_rows()[2]
                    self.assertEqual([r["closing_quantity"] for r in first], [r["closing_quantity"] for r in second])

    def test_audited_correction_preserves_original_run_and_reprocessing_is_idempotent(self):
        original_history = [
            self.event("E-RECEIPT-V1", "2026-01-01", "receipt", 10),
            self.event("E-ISSUE", "2026-01-02", "issue", 3),
        ]
        self.write_events(original_history)
        original_snapshot_text = self.input.read_text(encoding="utf-8")
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        first_pointer, first_dir, first_balances = self.read_run_rows()
        self.assertEqual([int(row["closing_quantity"]) for row in first_balances], [10, 7])

        corrected_history = original_history + [
            self.event(
                "E-RECEIPT-V2", "2026-01-01", "receipt", 8,
                recorded_at="2026-01-03T10:00:00Z",
                supersedes_event_id="E-RECEIPT-V1",
            )
        ]
        self.write_events(corrected_history)
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        second_pointer, second_dir, corrected_balances = self.read_run_rows()
        self.assertEqual([int(row["closing_quantity"]) for row in corrected_balances], [8, 5])
        self.assertNotEqual(first_pointer["run_id"], second_pointer["run_id"])
        self.assertTrue(first_dir.exists())
        self.assertEqual(
            (first_dir / "input_snapshot/transactions.csv").read_text(encoding="utf-8"),
            original_snapshot_text,
        )

        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        third_pointer, _third_dir, rerun_balances = self.read_run_rows()
        self.assertNotEqual(second_pointer["run_id"], third_pointer["run_id"])
        self.assertEqual([int(row["closing_quantity"]) for row in rerun_balances], [8, 5])
        with (second_dir / "accepted_events.csv").open(newline="", encoding="utf-8") as handle:
            accepted = list(csv.DictReader(handle))
        self.assertEqual(
            {row["event_id"]: row["version_status"] for row in accepted}["E-RECEIPT-V1"],
            "superseded",
        )
        summary = json.loads((second_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["accepted_rows"], 3)
        self.assertEqual(summary["active_event_rows"], 2)
        self.assertEqual(summary["superseded_event_rows"], 1)

    def test_correcting_transfer_replaces_both_location_effects(self):
        self.write_events([
            self.event("E-RECEIPT", "2026-01-01", "receipt", 10),
            self.event(
                "E-TRANSFER-V1", "2026-01-02", "transfer", 4,
                location_id="", from_location="A", to_location="B",
            ),
            self.event(
                "E-TRANSFER-V2", "2026-01-02", "transfer", 2,
                location_id="", from_location="A", to_location="B",
                recorded_at="2026-01-03T10:00:00Z",
                supersedes_event_id="E-TRANSFER-V1",
            ),
        ])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        active = [event for event in events if event["version_status"] == "active"]
        balances = compute_daily_balances(active, "run", "transactions.csv")
        result = reconcile(
            active,
            balances,
            input_rows=count,
            accepted_rows=len(events),
            rejected_rows=len(rejects),
        )
        latest = {
            row["location_id"]: row
            for row in balances
            if row["balance_date"] == "2026-01-02"
        }
        self.assertEqual(latest["A"]["closing_quantity"], 8)
        self.assertEqual(latest["B"]["closing_quantity"], 2)
        self.assertTrue(result["passed"])

    def test_branching_correction_versions_are_rejected_and_original_remains_active(self):
        self.write_events([
            self.event("E-ORIGINAL", "2026-01-01", "receipt", 10),
            self.event(
                "E-CORR-A", "2026-01-01", "receipt", 8,
                recorded_at="2026-01-02T10:00:00Z",
                supersedes_event_id="E-ORIGINAL",
            ),
            self.event(
                "E-CORR-B", "2026-01-01", "receipt", 7,
                recorded_at="2026-01-03T10:00:00Z",
                supersedes_event_id="E-ORIGINAL",
            ),
        ])
        events, rejects, count = read_and_validate(self.input, self.products, self.locations)
        self.assertEqual(count, 3)
        self.assertEqual([event["event_id"] for event in events], ["E-ORIGINAL"])
        self.assertEqual([event["version_status"] for event in events], ["active"])
        self.assertEqual([item["reason_code"] for item in rejects], [
            "conflicting_supersession", "conflicting_supersession"
        ])



    def test_prior_event_ids_cannot_be_edited_or_removed_between_runs(self):
        original = self.event("E-1", "2026-01-01", "receipt", 10)
        issue = self.event("E-2", "2026-01-02", "issue", 3)
        self.write_events([original, issue])
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        pointer = json.loads((self.output / "latest_success.json").read_text(encoding="utf-8"))
        successful_id = pointer["run_id"]

        edited = self.event("E-1", "2026-01-01", "receipt", 8)
        self.write_events([edited, issue])
        with self.assertLogs(level="ERROR"):
            self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 1)
        self.assertEqual(
            json.loads((self.output / "latest_success.json").read_text(encoding="utf-8"))["run_id"],
            successful_id,
        )
        failed_dir = next((self.output / "runs").glob("failed-*"))
        failed = json.loads((failed_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["error"]["stage"], "validate_history")
        self.assertIn("event IDs are immutable", failed["error"]["message"])

        correction = self.event(
            "E-1-V2", "2026-01-01", "receipt", 8,
            recorded_at="2026-01-03T10:00:00Z",
            supersedes_event_id="E-1",
        )
        self.write_events([original, issue, correction])
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        pointer = json.loads((self.output / "latest_success.json").read_text(encoding="utf-8"))
        run_dir = self.output / Path(pointer["manifest"]).parent
        with (run_dir / "daily_balances.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([int(row["closing_quantity"]) for row in rows], [8, 5])



if __name__ == "__main__":
    unittest.main()
