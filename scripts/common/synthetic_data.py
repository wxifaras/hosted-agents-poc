"""Deterministic synthetic sales data with an independent expected-results oracle.

Stdlib-only so the same bytes are produced on any developer machine. Rows are drawn
from one seeded stream, so a smaller dataset is always a prefix of a larger one.
"""

import csv
import hashlib
import io
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta

SEED = 20261022
DATASET_SIZES: tuple[int, ...] = (1_000, 10_000, 100_000)
COLUMNS: tuple[str, ...] = (
    "row_id",
    "order_date",
    "region",
    "category",
    "units",
    "unit_price_cents",
    "revenue_cents",
    "status",
)
REGIONS: tuple[str, ...] = ("North", "South", "East", "West")
CATEGORIES: tuple[str, ...] = ("Accessories", "Hardware", "Services", "Software", "Training")
STATUSES: tuple[str, ...] = ("completed", "completed", "completed", "pending", "cancelled", "returned")
FILTER_REGION = "West"
FILTER_STATUS = "completed"
_START = date(2026, 1, 1)


@dataclass(frozen=True)
class Row:
    """One synthetic order; ``units`` is occasionally missing to exercise null handling."""

    row_id: int
    order_date: date
    region: str
    category: str
    units: int | None
    unit_price_cents: int
    status: str

    @property
    def revenue_cents(self) -> int | None:
        return None if self.units is None else self.units * self.unit_price_cents


@dataclass(frozen=True)
class Dataset:
    """Serialized CSV bytes plus the identifiers used to prove what the sandbox read."""

    rows: int
    filename: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class CategoryTotals:
    rows: int
    units: int
    revenue_cents: int


def generate_rows(count: int, *, seed: int = SEED) -> Iterator[Row]:
    """Yield ``count`` rows; the stream is identical for every size with the same seed."""
    if count <= 0:
        raise ValueError("Row count must be positive.")
    rng = random.Random(seed)
    for index in range(1, count + 1):
        units: int | None = rng.randint(1, 20)
        if rng.random() < 0.02:
            units = None
        yield Row(
            row_id=index,
            order_date=_START + timedelta(days=rng.randrange(365)),
            region=rng.choice(REGIONS),
            category=rng.choice(CATEGORIES),
            units=units,
            unit_price_cents=rng.randrange(199, 50_000),
            status=rng.choice(STATUSES),
        )


def to_csv(rows: Iterable[Row]) -> bytes:
    """Serialize with LF line endings and empty strings for nulls."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(
            (
                row.row_id,
                row.order_date.isoformat(),
                row.region,
                row.category,
                "" if row.units is None else row.units,
                row.unit_price_cents,
                "" if row.revenue_cents is None else row.revenue_cents,
                row.status,
            )
        )
    return buffer.getvalue().encode("utf-8")


def build_dataset(count: int, *, seed: int = SEED) -> Dataset:
    return Dataset(rows=count, filename=f"story1-sales-{count}.csv", content=to_csv(generate_rows(count, seed=seed)))


def expected_aggregates(rows: Iterable[Row]) -> dict[str, CategoryTotals]:
    """Filter completed West rows, then group by category; nulls are excluded from sums."""
    totals: dict[str, list[int]] = {}
    for row in rows:
        if row.status != FILTER_STATUS or row.region != FILTER_REGION:
            continue
        current = totals.setdefault(row.category, [0, 0, 0])
        current[0] += 1
        if row.units is not None and row.revenue_cents is not None:
            current[1] += row.units
            current[2] += row.revenue_cents
    return {name: CategoryTotals(*values) for name, values in sorted(totals.items())}
