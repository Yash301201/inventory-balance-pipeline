import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from generate_sample import generate, generate_scenario
from main import run_pipeline


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "work"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.products = self.root / "products.csv"
        self.locations = self.root / "locations.csv"
        self.products.write_text("product_id,product_name\nP9,Custom product\n", encoding="utf-8")
        self.locations.write_text(
            "location_id,location_name\nX,Custom X\nY,Custom Y\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def read_csv(path):
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_fixed_seed_clean_generator_is_reproducible_and_uses_masters(self):
        first = self.root / "first.csv"
        second = self.root / "second.csv"
        generate(250, 42, first, self.products, self.locations)
        generate(250, 42, second, self.products, self.locations)
        first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
        second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
        self.assertEqual(first_hash, second_hash)
        rows = self.read_csv(first)
        self.assertEqual(len(rows), 250)
        self.assertTrue({row["product_id"] for row in rows} <= {"P9"})
        self.assertTrue(
            {row[key] for row in rows for key in ("location_id", "from_location", "to_location") if row[key]}
            <= {"X", "Y"}
        )

    def test_scenario_labels_manifest_and_independent_balance_fixture_match_pipeline(self):
        transactions = self.root / "scenario.csv"
        labels = self.root / "scenario.labels.csv"
        manifest_path = self.root / "scenario.manifest.json"
        expected_path = self.root / "scenario.expected.csv"
        artifacts = generate_scenario(
            transactions,
            labels_output=labels,
            manifest_output=manifest_path,
            expected_output=expected_path,
            products_path=self.products,
            locations_path=self.locations,
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        labels_rows = self.read_csv(labels)
        self.assertTrue(manifest["labels_are_not_pipeline_input"])
        self.assertEqual(len(labels_rows), manifest["rows"])
        scenario_counts = {}
        disposition_counts = {}
        for label in labels_rows:
            scenario_counts[label["scenario_id"]] = scenario_counts.get(label["scenario_id"], 0) + 1
            disposition = label["expected_disposition"]
            disposition_counts[disposition] = disposition_counts.get(disposition, 0) + 1
        self.assertEqual(scenario_counts, manifest["scenario_counts"])
        self.assertEqual(disposition_counts, manifest["expected_dispositions"])
        self.assertEqual(
            hashlib.sha256(transactions.read_bytes()).hexdigest(),
            manifest["sha256"],
        )
        second = generate_scenario(
            self.root / "repeat.csv",
            products_path=self.products,
            locations_path=self.locations,
        )
        self.assertEqual(transactions.read_bytes(), second["events_path"].read_bytes())
        self.assertEqual(labels.read_bytes(), second["labels_path"].read_bytes())
        self.assertEqual(expected_path.read_bytes(), second["expected_path"].read_bytes())

        output = self.root / "output"
        self.assertEqual(
            run_pipeline(transactions, self.products, self.locations, output),
            0,
        )
        pointer = json.loads((output / "latest_success.json").read_text(encoding="utf-8"))
        run_dir = output / Path(pointer["manifest"]).parent
        accepted = self.read_csv(run_dir / "accepted_events.csv")
        rejected = self.read_csv(run_dir / "rejected_records.csv")
        actual_by_record = {
            row["source_record"]: "accepted_" + row["version_status"]
            for row in accepted
        }
        actual_reason = {row["source_record"]: row["reason_code"] for row in rejected}
        for label in labels_rows:
            record = label["source_record"]
            if label["expected_disposition"] == "rejected":
                self.assertEqual(actual_reason[record], label["expected_reason_code"])
            else:
                self.assertEqual(actual_by_record[record], label["expected_disposition"])

        actual_balances = self.read_csv(run_dir / "daily_balances.csv")
        expected_balances = self.read_csv(expected_path)
        self.assertEqual(
            [
                (r["balance_date"], r["product_id"], r["location_id"], r["closing_quantity"])
                for r in actual_balances
            ],
            [
                (r["balance_date"], r["product_id"], r["location_id"], r["closing_quantity"])
                for r in expected_balances
            ],
        )
        self.assertEqual(artifacts["rows"], 11)


if __name__ == "__main__":
    unittest.main()
