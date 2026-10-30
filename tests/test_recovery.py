import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import main as pipeline_main
from main import run_pipeline
from validate import TRANSACTION_FIELDS


class RecoveryTests(unittest.TestCase):
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
    def event(event_id="E1", event_date="2026-01-01", quantity=5):
        return {
            "event_id": event_id,
            "event_date": event_date,
            "product_id": "P1",
            "event_type": "receipt",
            "quantity": quantity,
            "location_id": "A",
            "from_location": "",
            "to_location": "",
            "recorded_at": "",
            "supersedes_event_id": "",
        }

    def write_events(self, rows):
        with self.input.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def latest(self):
        return json.loads((self.output / "latest_success.json").read_text(encoding="utf-8"))

    def run_dir(self, pointer):
        return self.output / Path(pointer["manifest"]).parent

    def failed_runs(self):
        return list((self.output / "runs").glob("failed-*"))

    def test_second_artifact_failure_keeps_previous_success_as_latest(self):
        self.write_events([self.event()])
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        first_pointer = self.latest()
        first_dir = self.run_dir(first_pointer)
        original_write_csv = pipeline_main.write_csv

        def fail_on_reject_file(path, fields, rows):
            if path.name == "rejected_records.csv":
                raise OSError("simulated second artifact failure")
            original_write_csv(path, fields, rows)

        with patch("main.write_csv", side_effect=fail_on_reject_file):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 1)

        self.assertEqual(self.latest()["run_id"], first_pointer["run_id"])
        self.assertTrue((first_dir / "daily_balances.csv").exists())
        self.assertTrue((first_dir / "rejected_records.csv").exists())
        failed_dir = self.failed_runs()[-1]
        failed_summary = json.loads((failed_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(failed_summary["status"], "failed")
        self.assertEqual(failed_summary["error"]["stage"], "write_artifacts")
        self.assertEqual(failed_summary["input_rows"], 1)
        self.assertEqual(failed_summary["accepted_rows"], 1)
        self.assertEqual(failed_summary["calculated_balance_rows"], 1)

        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        self.assertNotEqual(self.latest()["run_id"], first_pointer["run_id"])

    def test_summary_write_failure_does_not_change_latest_success(self):
        self.write_events([self.event()])
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        previous_id = self.latest()["run_id"]
        original_writer = pipeline_main.write_json_artifact

        def fail_success_summary(path, payload):
            if path.name == "run_summary.json" and payload.get("status") != "failed":
                raise OSError("simulated summary write failure")
            original_writer(path, payload)

        with patch("main.write_json_artifact", side_effect=fail_success_summary):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 1)
        self.assertEqual(self.latest()["run_id"], previous_id)
        failed = json.loads((self.failed_runs()[-1] / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["error"]["stage"], "write_summary")
        self.assertEqual(failed["error"]["message"], "simulated summary write failure")

    def test_manifest_write_failure_does_not_change_latest_success(self):
        self.write_events([self.event()])
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        previous_id = self.latest()["run_id"]
        original_writer = pipeline_main.write_json_artifact

        def fail_manifest(path, payload):
            if path.name == "run_manifest.json":
                raise OSError("simulated manifest write failure")
            original_writer(path, payload)

        with patch("main.write_json_artifact", side_effect=fail_manifest):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 1)
        self.assertEqual(self.latest()["run_id"], previous_id)
        failed = json.loads((self.failed_runs()[-1] / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["error"]["stage"], "build_manifest")

    def test_latest_pointer_failure_leaves_previous_success_active(self):
        self.write_events([self.event()])
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        previous_id = self.latest()["run_id"]
        with patch("main.publish_latest_success", side_effect=OSError("simulated pointer failure")):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 1)
        self.assertEqual(self.latest()["run_id"], previous_id)
        failed_dir = self.failed_runs()[-1]
        failure_summary = json.loads((failed_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(failure_summary["error"]["stage"], "publish_latest_success")
        failure_manifest = json.loads((failed_dir / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(failure_manifest["run_status"], "failed")
        self.assertEqual(failure_manifest["publication_status"], "not_published")

    def test_invalid_utf8_failure_is_recorded_with_type_and_stage(self):
        self.write_events([self.event()])
        self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 0)
        previous_id = self.latest()["run_id"]
        self.input.write_bytes(b"event_id,event_date,product_id,event_type,quantity,location_id,from_location,to_location,recorded_at,supersedes_event_id\nE1,\xff")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 1)
        self.assertEqual(self.latest()["run_id"], previous_id)
        failure_summary = json.loads((self.failed_runs()[-1] / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(failure_summary["error"]["type"], "UnicodeDecodeError")
        self.assertEqual(failure_summary["error"]["stage"], "validate")
        self.assertEqual(failure_summary["input_rows"], 0)



    def test_transform_failure_keeps_prior_latest_and_reports_known_stage_counts(self):
        self.write_events([
            self.event("E1", "2000-01-01", quantity=5),
            self.event("E2", "2026-01-01", quantity=5),
        ])
        with self.assertLogs(level="ERROR"):
            self.assertEqual(run_pipeline(self.input, self.products, self.locations, self.output), 1)
        failed_dir = self.failed_runs()[-1]
        summary = json.loads((failed_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["error"]["stage"], "transform")
        self.assertEqual(summary["error"]["type"], "ValueError")
        self.assertEqual(summary["input_rows"], 2)
        self.assertEqual(summary["accepted_rows"], 2)
        self.assertEqual(summary["rejected_rows"], 0)
        self.assertEqual(summary["calculated_balance_rows"], 0)
        self.assertFalse((self.output / "latest_success.json").exists())


if __name__ == "__main__":
    unittest.main()
