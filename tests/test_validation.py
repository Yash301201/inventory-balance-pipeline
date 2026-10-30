import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from transform import compute_daily_balances
from validate import InputSchemaError, TRANSACTION_FIELDS, load_reference_keys, read_and_validate


class ValidationTests(unittest.TestCase):
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

    @staticmethod
    def event(event_id="E1", event_date="2026-01-01", product_id="P1",
              event_type="receipt", quantity="5", location_id="A",
              from_location="", to_location="", recorded_at="", supersedes_event_id=""):
        return [
            event_id, event_date, product_id, event_type, str(quantity), location_id,
            from_location, to_location, recorded_at, supersedes_event_id,
        ]

    def write_rows(self, rows):
        with self.input.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(TRANSACTION_FIELDS)
            writer.writerows(rows)

    def validate(self):
        return read_and_validate(self.input, self.products, self.locations)

    def test_unknown_product_location_and_transfer_endpoint_are_rejected(self):
        self.write_rows([
            self.event(product_id="UNKNOWN"),
            self.event(event_id="E2", location_id="UNKNOWN"),
            self.event(
                event_id="E3", event_type="transfer", quantity=1, location_id="",
                from_location="A", to_location="UNKNOWN",
            ),
        ])
        events, rejected, _ = self.validate()
        self.assertEqual(events, [])
        self.assertEqual(
            [item["reason_code"] for item in rejected],
            ["unknown_product", "unknown_location", "unknown_transfer_location"],
        )

    def test_master_files_reject_duplicate_or_blank_keys_and_changed_headers(self):
        self.products.write_text("product_id,product_name\nP1,One\nP1,Duplicate\n", encoding="utf-8")
        with self.assertRaisesRegex(InputSchemaError, "duplicate"):
            load_reference_keys(self.products, "product_id")
        self.products.write_text("product_id,product_name\n,Blank\n", encoding="utf-8")
        with self.assertRaisesRegex(InputSchemaError, "empty"):
            load_reference_keys(self.products, "product_id")
        self.products.write_text("product_id,product_name,extra\nP1,One,x\n", encoding="utf-8")
        with self.assertRaisesRegex(InputSchemaError, "header"):
            load_reference_keys(self.products, "product_id")
        self.products.write_text("product_id,product_id\nP1,One\n", encoding="utf-8")
        with self.assertRaisesRegex(InputSchemaError, "duplicate column"):
            load_reference_keys(self.products, "product_id")

    def test_movement_quantity_rules_and_event_types(self):
        self.write_rows([
            self.event(event_id="E1", quantity="1.5"),
            self.event(event_id="E2", quantity="0"),
            self.event(event_id="E3", event_type="issue", quantity="-1"),
            self.event(event_id="E4", event_type="adjustment", quantity="0"),
            self.event(event_id="E5", event_type="adjustment", quantity="2"),
            self.event(event_id="E6", event_type="adjustment", quantity="-3"),
            self.event(event_id="E7", event_type="adjustment", quantity="0.5"),
            self.event(event_id="E8", event_type="mystery", quantity=1),
        ])
        events, rejected, _ = self.validate()
        self.assertEqual([(e["event_id"], e["quantity"]) for e in events], [("E5", 2), ("E6", -3)])
        self.assertEqual(
            [item["reason_code"] for item in rejected],
            [
                "invalid_quantity", "invalid_movement_quantity", "invalid_movement_quantity",
                "zero_adjustment", "invalid_quantity", "invalid_event_type",
            ],
        )

    def test_transfer_rules_require_two_distinct_known_locations(self):
        self.write_rows([
            self.event(event_id="E1", event_type="transfer", location_id="",
                       from_location="A", to_location=""),
            self.event(event_id="E2", event_type="transfer", location_id="",
                       from_location="A", to_location="A"),
            self.event(event_id="E3", event_type="transfer", location_id="B",
                       from_location="A", to_location="B"),
        ])
        events, rejected, _ = self.validate()
        self.assertEqual(events, [])
        self.assertEqual(
            [item["reason_code"] for item in rejected],
            ["missing_transfer_endpoint", "same_location_transfer", "invalid_transfer_location_fields"],
        )

    def test_short_and_extra_records_are_rejected_and_extra_values_are_preserved(self):
        extra = self.event()
        extra.append("tail-value")
        short = self.event("E2")[:-1]
        self.write_rows([extra, short])
        events, rejected, _ = self.validate()
        self.assertEqual(events, [])
        self.assertEqual([item["reason_code"] for item in rejected], ["extra_values", "short_record"])
        self.assertEqual(rejected[0]["extra_values"], ["tail-value"])
        self.assertEqual(rejected[0]["raw_event"]["supersedes_event_id"], "")
        self.assertIsNone(rejected[1]["raw_event"]["supersedes_event_id"])

    def test_malformed_csv_quotes_fail_the_batch(self):
        self.input.write_text(
            ",".join(TRANSACTION_FIELDS) + '\nE1,2026-01-01,P1,receipt,5,"A',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(InputSchemaError, "malformed CSV"):
            self.validate()

    def test_valid_quoted_and_multiline_fields_keep_logical_record_and_physical_lines(self):
        row = self.event(event_id="E,1")
        row[0] = "E\n1"
        self.write_rows([row])
        events, rejected, count = self.validate()
        self.assertEqual((len(events), len(rejected), count), (1, 0, 1))
        self.assertEqual(events[0]["source_record"], 1)
        self.assertEqual((events[0]["line_start"], events[0]["line_end"]), (2, 3))

    def test_duplicate_transaction_headers_are_rejected(self):
        self.input.write_text("event_id,event_id," + ",".join(TRANSACTION_FIELDS[2:]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(InputSchemaError, "duplicate column"):
            self.validate()

    def test_zero_byte_and_header_only_files_have_distinct_outcomes(self):
        self.input.write_bytes(b"")
        with self.assertRaisesRegex(InputSchemaError, "zero bytes"):
            self.validate()
        self.input.write_text(",".join(TRANSACTION_FIELDS) + "\n", encoding="utf-8")
        events, rejected, count = self.validate()
        self.assertEqual((events, rejected, count), ([], [], 0))

    def test_maximum_iso_date_is_safe_and_calendar_span_is_bounded(self):
        self.write_rows([self.event(event_date="9999-12-31")])
        events, rejected, _ = self.validate()
        self.assertEqual((len(events), len(rejected)), (1, 0))
        rows = compute_daily_balances(events, "run", "transactions.csv")
        self.assertEqual(rows[0]["balance_date"], "9999-12-31")
        self.write_rows([
            self.event(event_id="EARLY", event_date="2000-01-01"),
            self.event(event_id="LATE", event_date="2026-01-01"),
        ])
        events, _, _ = self.validate()
        with self.assertRaisesRegex(ValueError, "maximum is"):
            compute_daily_balances(events, "run", "transactions.csv", max_calendar_days=3660)



    def test_dense_grid_row_limit_is_enforced(self):
        self.write_rows([
            self.event(event_id="E1", event_date="2026-01-01", product_id="P1", location_id="A"),
            self.event(event_id="E2", event_date="2026-01-02", product_id="P2", location_id="B"),
        ])
        events, rejected, _ = self.validate()
        self.assertEqual(rejected, [])
        with self.assertRaisesRegex(ValueError, "would contain 4 rows"):
            compute_daily_balances(
                events,
                "run",
                "transactions.csv",
                max_output_rows=3,
            )



    def test_quote_inside_unquoted_field_is_rejected(self):
        self.input.write_text(
            ",".join(TRANSACTION_FIELDS) + '\nE"1,2026-01-01,P1,receipt,5,A,,,,\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(InputSchemaError, "malformed CSV quoting"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
