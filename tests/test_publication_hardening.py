import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import main
from publish import OutputLock

class PublicationHardeningTests(unittest.TestCase):
    def test_overlapping_writer_fails_and_different_root_is_independent(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "work") as temp:
            root = Path(temp)
            lock = OutputLock(root / "a")
            lock.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    OutputLock(root / "a").acquire()
                self.assertEqual(main.run_pipeline(ROOT / "data/input/transactions.csv", ROOT / "data/input/products.csv", ROOT / "data/input/locations.csv", root / "a"), 1)
                self.assertFalse((root / "a/latest_success.json").exists())
                self.assertEqual(main.run_pipeline(ROOT / "data/input/transactions.csv", ROOT / "data/input/products.csv", ROOT / "data/input/locations.csv", root / "b"), 0)
            finally:
                lock.release()
            self.assertFalse(lock.path.exists())

    def test_altered_persisted_csv_cannot_activate(self):
        original = main.write_csv
        def corrupt(path, fields, rows):
            original(path, fields, rows)
            if path.name == "daily_balances.csv":
                text = path.read_text()
                path.write_text(text.replace(",P1,", ",CORRUPTED,", 1) if ",P1," in text else text.replace("2026-", "2025-", 1))
        with tempfile.TemporaryDirectory(dir=ROOT / "work") as temp, patch.object(main, "write_csv", side_effect=corrupt):
            output = Path(temp)
            self.assertEqual(main.run_pipeline(ROOT / "data/input/transactions.csv", ROOT / "data/input/products.csv", ROOT / "data/input/locations.csv", output), 1)
            self.assertFalse((output / "latest_success.json").exists())

