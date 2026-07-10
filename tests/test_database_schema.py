from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import (
    DATABASE_PATH,
    INVENTORY_VALUE_COLUMN,
    get_product_schema_description,
    validate_product_schema,
)


class DatabaseSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test_inventory.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _create_product_table(self, schema_sql: str) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(schema_sql)

    def test_database_path_points_to_product_inventory_file(self) -> None:
        self.assertEqual(DATABASE_PATH.name, "product_inventory.db")

    def test_validate_product_schema_accepts_the_expected_stock_column(self) -> None:
        self._create_product_table(
            """
            CREATE TABLE PRODUCT (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                NAME TEXT,
                CATEGORY TEXT,
                BRAND TEXT,
                PRICE REAL,
                STOCK INTEGER,
                SIZE TEXT,
                COLOR TEXT,
                WEIGHT REAL,
                SPECIFICATIONS TEXT
            )
            """.replace("STOCK", INVENTORY_VALUE_COLUMN)
        )

        validate_product_schema(self.db_path)

    def test_validate_product_schema_rejects_the_legacy_quantity_schema(self) -> None:
        self._create_product_table(
            """
            CREATE TABLE PRODUCT (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                NAME TEXT,
                CATEGORY TEXT,
                BRAND TEXT,
                PRICE REAL,
                QUANTITY INTEGER,
                SIZE TEXT,
                COLOR TEXT,
                WEIGHT REAL,
                SPECIFICATIONS TEXT
            )
            """
        )

        with self.assertRaisesRegex(RuntimeError, "STOCK"):
            validate_product_schema(self.db_path)

    def test_get_product_schema_description_includes_all_required_columns(self) -> None:
        description = get_product_schema_description()
        self.assertIn("PRODUCT", description)
        for column in ("NAME", "CATEGORY", "BRAND", "PRICE", "STOCK", "SIZE", "COLOR", "WEIGHT", "SPECIFICATIONS"):
            self.assertIn(column, description)

    def test_get_product_schema_description_uses_live_columns(self) -> None:
        self._create_product_table(
            """
            CREATE TABLE PRODUCT (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                NAME TEXT,
                CATEGORY TEXT,
                BRAND TEXT,
                PRICE REAL,
                STOCK INTEGER,
                SIZE TEXT,
                COLOR TEXT,
                WEIGHT REAL,
                SPECIFICATIONS TEXT
            )
            """
        )
        description = get_product_schema_description(self.db_path)
        self.assertIn("BRAND", description)
        self.assertIn("SPECIFICATIONS", description)


class AppSourceTests(unittest.TestCase):
    def test_dashboard_startup_query_uses_the_shared_stock_schema(self) -> None:
        app_source = Path("app.py").read_text()
        self.assertIn("DATABASE_PATH", app_source)
        self.assertIn("validate_product_schema", app_source)
        self.assertIn("INVENTORY_VALUE_COLUMN", app_source)
        self.assertIn("PRODUCT_TABLE", app_source)
        self.assertIn("get_product_schema_description", app_source)
        self.assertIn("COALESCE(SUM(price *", app_source)
        self.assertNotIn("db_path = 'inventory.db'", app_source)
        self.assertNotIn("quantity", app_source.lower())


if __name__ == "__main__":
    unittest.main()
