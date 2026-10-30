"""Create deterministic synthetic inventory transactions."""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

from validate import TRANSACTION_FIELDS


def generate(rows: int, seed: int, output: Path) -> None:
    if rows < 0:
        raise ValueError("--rows must be zero or greater")
    output.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    # Use the product and location keys supplied in the starter reference files.
    products = ["SKU-001", "SKU-002"]
    locations = ["WH-A", "WH-B"]
    start = date(2026, 1, 1)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
        writer.writeheader()
        for index in range(rows):
            event_type = rng.choices(
                ["receipt", "issue", "transfer", "adjustment"],
                weights=[40, 30, 15, 15],
                k=1,
            )[0]
            day = start + timedelta(days=rng.randrange(365))
            product = rng.choice(products)
            quantity = rng.randint(1, 20)
            row = {
                "event_id": f"GEN-{index + 1:09d}",
                "event_date": day.isoformat(),
                "product_id": product,
                "event_type": event_type,
                "quantity": quantity,
                "location_id": "",
                "from_location": "",
                "to_location": "",
            }
            if event_type in {"receipt", "issue"}:
                row["location_id"] = rng.choice(locations)
            elif event_type == "adjustment":
                row["location_id"] = rng.choice(locations)
                row["quantity"] = quantity if rng.choice([True, False]) else -quantity
            else:
                source, target = rng.sample(locations, 2)
                row["from_location"] = source
                row["to_location"] = target
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("data/input/large_transactions.csv"))
    args = parser.parse_args()
    generate(args.rows, args.seed, args.output)
    print(f"Wrote {args.rows:,} deterministic events to {args.output}")


if __name__ == "__main__":
    main()
