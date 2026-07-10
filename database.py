"""Database configuration and schema helpers for the inventory app."""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from pathlib import Path

DATABASE_PATH = Path(__file__).with_name("product_inventory.db")
PRODUCT_TABLE = "PRODUCT"
INVENTORY_VALUE_COLUMN = "STOCK"
PRODUCT_REQUIRED_COLUMNS = (
    "ID",
    "NAME",
    "CATEGORY",
    "BRAND",
    "PRICE",
    INVENTORY_VALUE_COLUMN,
    "SIZE",
    "COLOR",
    "WEIGHT",
    "SPECIFICATIONS",
)

CREATE_PRODUCT_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {PRODUCT_TABLE} (
    ID INTEGER PRIMARY KEY AUTOINCREMENT,
    NAME VARCHAR(100),
    CATEGORY VARCHAR(50),
    BRAND VARCHAR(50),
    PRICE REAL,
    {INVENTORY_VALUE_COLUMN} INTEGER,
    SIZE VARCHAR(20),
    COLOR VARCHAR(20),
    WEIGHT REAL,
    SPECIFICATIONS TEXT
);
"""

def get_connection(db_path: str | Path = DATABASE_PATH) -> sqlite3.Connection:
    """Return a SQLite connection for the configured product database."""

    return sqlite3.connect(str(db_path))


def _get_schema_version(connection: sqlite3.Connection) -> int:
    """Return the current schema version stored in the database.

    Returns 0 for databases that pre-date the versioning system so that all
    migrations are applied on the next ``ensure_schema`` call.
    """

    connection.execute(
        "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)"
    )
    row = connection.execute("SELECT MAX(version) FROM _schema_version").fetchone()
    return row[0] if row[0] is not None else 0


def _set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute("DELETE FROM _schema_version")
    connection.execute("INSERT INTO _schema_version (version) VALUES (?)", (version,))


def _product_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (PRODUCT_TABLE,),
    ).fetchone()
    return row is not None


def _get_product_columns(connection: sqlite3.Connection) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({PRODUCT_TABLE})").fetchall()]


def _raise_for_missing_columns(actual_columns: list[str]) -> None:
    missing_columns = [column for column in PRODUCT_REQUIRED_COLUMNS if column not in actual_columns]
    if missing_columns:
        raise RuntimeError(
            "The PRODUCT table schema is out of date. "
            f"Missing columns: {', '.join(missing_columns)}. "
            f"Expected columns: {', '.join(PRODUCT_REQUIRED_COLUMNS)}."
        )


_PRODUCT_COLUMN_SQL_TYPES = {
    "ID": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "NAME": "TEXT",
    "CATEGORY": "TEXT",
    "BRAND": "TEXT",
    "PRICE": "REAL",
    INVENTORY_VALUE_COLUMN: "INTEGER",
    "SIZE": "TEXT",
    "COLOR": "TEXT",
    "WEIGHT": "REAL",
    "SPECIFICATIONS": "TEXT",
}


def get_product_schema_description(db_path: str | Path | None = None) -> str:
    """Return a full PRODUCT schema description for NL→SQL prompts.

    Prefers live column names from ``db_path`` when the table exists; otherwise
    falls back to ``PRODUCT_REQUIRED_COLUMNS``.
    """

    columns: list[str] = list(PRODUCT_REQUIRED_COLUMNS)
    if db_path is not None:
        try:
            with get_connection(db_path) as connection:
                if _product_table_exists(connection):
                    live_columns = _get_product_columns(connection)
                    if live_columns:
                        columns = live_columns
        except Exception:
            columns = list(PRODUCT_REQUIRED_COLUMNS)

    rendered = ", ".join(
        f"{column} {_PRODUCT_COLUMN_SQL_TYPES.get(str(column).upper(), 'TEXT')}"
        for column in columns
    )
    return f"Product table schema: {PRODUCT_TABLE} ({rendered})"


def _ensure_product_table_matches_current_schema(connection: sqlite3.Connection) -> None:
    """Create or repair the PRODUCT table so it matches the current schema."""

    if not _product_table_exists(connection):
        connection.execute(CREATE_PRODUCT_TABLE_SQL)
        return

    actual_columns = _get_product_columns(connection)
    if INVENTORY_VALUE_COLUMN not in actual_columns and "QUANTITY" in actual_columns:
        connection.execute(
            f"ALTER TABLE {PRODUCT_TABLE} RENAME COLUMN QUANTITY TO {INVENTORY_VALUE_COLUMN}"
        )
        actual_columns = _get_product_columns(connection)

    _raise_for_missing_columns(actual_columns)


# Ordered migration steps: (version, function).
# To evolve the schema append a new tuple with the next version number and a
# forward-only migration function. Never edit or remove an existing entry —
# that would break databases already at that version.
_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _ensure_product_table_matches_current_schema),
]


def validate_product_schema(db_path: str | Path = DATABASE_PATH) -> None:
    """Raise a helpful error when the PRODUCT table schema does not match the app."""

    with get_connection(db_path) as connection:
        if not _product_table_exists(connection):
            raise RuntimeError(
                f"{db_path} does not contain the required {PRODUCT_TABLE} table."
            )
        actual_columns = _get_product_columns(connection)

    _raise_for_missing_columns(actual_columns)


def _build_fake() -> object:
    """Return a Faker-like object, falling back to a lightweight stub if needed."""

    try:
        from faker import Faker as _Faker
    except ImportError:
        class _FallbackFake:
            def word(self) -> str:
                return random.choice(["alpha", "beta", "gamma", "delta"])

            def company(self) -> str:
                return random.choice(["Acme Corp", "Northwind", "Globex", "Initech"])

            def color_name(self) -> str:
                return random.choice(["Red", "Blue", "Green", "Black", "White"])

            def text(self, max_nb_chars: int = 200) -> str:
                return "Sample product description."

        return _FallbackFake()

    return _Faker()


def generate_product_name(fake: object) -> str:
    """Generate a realistic product name for the sample dataset."""

    adjectives = [
        "Premium",
        "Deluxe",
        "Advanced",
        "Smart",
        "Eco-friendly",
        "Compact",
        "Portable",
        "Professional",
    ]
    nouns = ["Device", "Gadget", "Tool", "Appliance", "System", "Kit", "Set", "Solution"]
    return f"{random.choice(adjectives)} {fake.word().capitalize()} {random.choice(nouns)}"


def generate_product_data(num_products: int) -> list[tuple]:
    """Build a sample inventory dataset matching the PRODUCT schema."""

    fake = _build_fake()
    categories = [
        "Electronics",
        "Clothing",
        "Home & Garden",
        "Sports & Outdoors",
        "Books",
        "Toys",
        "Beauty",
        "Food & Beverage",
    ]
    sizes = ["XS", "S", "M", "L", "XL", "XXL", "N/A"]
    product_data = []

    for _ in range(num_products):
        product_data.append(
            (
                generate_product_name(fake),
                random.choice(categories),
                fake.company(),
                round(random.uniform(1.0, 1000.0), 2),
                random.randint(0, 1000),
                random.choice(sizes),
                fake.color_name(),
                round(random.uniform(0.1, 50.0), 2),
                fake.text(max_nb_chars=200),
            )
        )

    return product_data


def ensure_schema(db_path: str | Path = DATABASE_PATH) -> None:
    """Apply any pending schema migrations in version order.

    Safe to call on every application startup: it is non-destructive and
    idempotent. Existing rows are never touched. Future schema changes should
    be added as new entries in ``_MIGRATIONS`` rather than editing old ones.
    """

    with get_connection(db_path) as connection:
        current_version = _get_schema_version(connection)
        for version, migration in _MIGRATIONS:
            if version > current_version:
                migration(connection)
                _set_schema_version(connection, version)
                current_version = version

        # Repair known legacy layouts even if version metadata was previously
        # advanced before the underlying table was actually upgraded.
        if current_version > 0:
            _ensure_product_table_matches_current_schema(connection)


def seed_database(
    db_path: str | Path = DATABASE_PATH,
    num_products: int = 10000,
    *,
    force: bool = False,
) -> None:
    """Populate the PRODUCT table with generated demo rows.

    This is an explicit, opt-in step — it must not be called automatically
    during schema bootstrap. By default the function is a no-op when the
    table already contains rows, preventing accidental duplication on repeated
    invocations. Pass ``force=True`` to insert rows regardless.

    Must be called after ``ensure_schema``.
    """

    with get_connection(db_path) as connection:
        if not force:
            existing = connection.execute(
                f"SELECT COUNT(*) FROM {PRODUCT_TABLE}"
            ).fetchone()[0]
            if existing > 0:
                return

    product_data = generate_product_data(num_products)
    with get_connection(db_path) as connection:
        cursor = connection.cursor()
        cursor.executemany(
            f"""
            INSERT INTO {PRODUCT_TABLE}
            (NAME, CATEGORY, BRAND, PRICE, {INVENTORY_VALUE_COLUMN}, SIZE, COLOR, WEIGHT, SPECIFICATIONS)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            product_data,
        )


def initialize_database(db_path: str | Path = DATABASE_PATH, num_products: int = 10000) -> None:
    """Ensure the PRODUCT table schema is present.

    Applies any pending migrations via ``ensure_schema``. Demo-data seeding is
    intentionally **not** part of bootstrap; call ``seed_database`` explicitly
    when sample rows are needed (e.g. on a fresh installation).

    ``num_products`` is accepted for API compatibility but is ignored here;
    pass it directly to ``seed_database`` when seeding.
    """

    ensure_schema(db_path)


def print_sample_rows(db_path: str | Path = DATABASE_PATH, limit: int = 10) -> None:
    """Print a few seeded rows to confirm the database contents."""

    with get_connection(db_path) as connection:
        cursor = connection.cursor()
        cursor.execute(f"SELECT * FROM {PRODUCT_TABLE} LIMIT ?", (limit,))
        for row in cursor.fetchall():
            print(row)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Inventory database management — schema migration and demo-data seeding."
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help=(
            "Populate the PRODUCT table with generated demo rows. "
            "Skipped automatically when rows already exist; use --force-seed to override."
        ),
    )
    parser.add_argument(
        "--force-seed",
        action="store_true",
        help="Seed demo rows even when the table already contains data (implies --seed).",
    )
    parser.add_argument(
        "--seed-count",
        type=int,
        default=10000,
        metavar="N",
        help="Number of demo rows to generate (default: 10 000). Requires --seed or --force-seed.",
    )
    args = parser.parse_args()

    ensure_schema()
    print(f"Schema is up to date at {DATABASE_PATH}.")

    if args.seed or args.force_seed:
        seed_database(num_products=args.seed_count, force=args.force_seed)
        print_sample_rows()
        print(f"Demo data loaded into {DATABASE_PATH}.")
