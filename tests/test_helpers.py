from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def temporary_working_directory(path: Path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


@contextmanager
def patched_modules(mapping):
    original = {}
    try:
        for name, module in mapping.items():
            original[name] = sys.modules.get(name)
            sys.modules[name] = module
        yield
    finally:
        for name, module in mapping.items():
            if original[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original[name]


class HelperSmokeTests(unittest.TestCase):
    def test_read_sql_query_handles_quantity_alias(self):
        from utils import read_sql_query

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "inventory.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE PRODUCT (
                        ID INTEGER PRIMARY KEY AUTOINCREMENT,
                        NAME TEXT,
                        PRICE REAL,
                        STOCK INTEGER
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO PRODUCT (NAME, PRICE, STOCK) VALUES (?, ?, ?)",
                    [("A", 2.5, 4), ("B", 1.5, 5)],
                )
                connection.commit()

            result = read_sql_query(
                "SELECT COUNT(*) as product_count, SUM(price * quantity) as total_inventory_value FROM PRODUCT",
                str(db_path),
            )

            self.assertEqual(result["product_count"].values[0], 2)
            self.assertAlmostEqual(result["total_inventory_value"].values[0], 17.5)

    def test_generate_sql_query_falls_back_to_inventory_schema(self):
        from prompt import generate_sql_query

        sql = generate_sql_query(
            "Product table schema: PRODUCT (ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME TEXT, PRICE REAL, STOCK INTEGER)",
            "What is the total inventory value?",
        )

        self.assertIn("SUM(PRICE * STOCK)", sql.upper())

    def test_process_excel_file_adds_columns_and_inserts_rows(self):
        fake_streamlit = types.ModuleType("streamlit")
        fake_streamlit.write = lambda *args, **kwargs: None

        fake_pandas = types.ModuleType("pandas")
        fake_pandas.read_excel = lambda uploaded_file: None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "inventory.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE PRODUCT (
                        ID INTEGER PRIMARY KEY AUTOINCREMENT,
                        NAME TEXT,
                        CATEGORY TEXT,
                        PRICE REAL,
                        STOCK INTEGER
                    )
                    """
                )
                connection.commit()

            class FakeColumns(list):
                def tolist(self):
                    return list(self)

            class FakeFrame:
                def __init__(self):
                    self.columns = FakeColumns(["Name", "Category", "Price", "Stock", "Color"])
                    self._rows = [
                        {
                            "Name": "Widget",
                            "Category": "Gadgets",
                            "Price": 9.99,
                            "Stock": 12,
                            "Color": "Blue",
                        }
                    ]

                def iterrows(self):
                    for index, row in enumerate(self._rows):
                        yield index, row

            with patched_modules({"streamlit": fake_streamlit, "pandas": fake_pandas}):
                excel_processing = importlib.import_module("excel_processing")
                excel_processing.pd.read_excel = lambda uploaded_file: FakeFrame()
                excel_processing.process_excel_file(
                    object(),
                    str(db_path),
                    "add",
                    allow_schema_changes=True,
                )

            with sqlite3.connect(db_path) as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(PRODUCT)")]
                self.assertIn("COLOR", columns)
                row = connection.execute(
                    "SELECT NAME, CATEGORY, PRICE, STOCK, COLOR FROM PRODUCT"
                ).fetchone()

            self.assertEqual(row, ("Widget", "Gadgets", 9.99, 12, "Blue"))

    def test_app_import_smoke(self):
        fake_streamlit = types.ModuleType("streamlit")

        class _ColumnContext:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_streamlit.set_page_config = lambda *args, **kwargs: None
        fake_streamlit.markdown = lambda *args, **kwargs: None
        fake_streamlit.columns = lambda count: tuple(_ColumnContext() for _ in range(count))
        fake_streamlit.text_area = lambda *args, **kwargs: ""
        fake_streamlit.button = lambda *args, **kwargs: False
        fake_streamlit.file_uploader = lambda *args, **kwargs: None
        fake_streamlit.selectbox = lambda *args, **kwargs: "add"
        fake_streamlit.checkbox = lambda *args, **kwargs: False
        fake_streamlit.session_state = {}
        fake_streamlit.text_input = lambda *args, **kwargs: ""
        fake_streamlit.pyplot = lambda *args, **kwargs: None
        fake_streamlit.write = lambda *args, **kwargs: None
        fake_streamlit.error = lambda *args, **kwargs: None
        fake_streamlit.success = lambda *args, **kwargs: None
        fake_streamlit.warning = lambda *args, **kwargs: None
        fake_streamlit.info = lambda *args, **kwargs: None

        fake_pandas = types.ModuleType("pandas")
        fake_pandas.read_excel = lambda uploaded_file: None

        fake_config = types.ModuleType("config")
        fake_config.GOOGLE_API_KEY = None
        fake_config.PANDASAI_API_KEY = None
        fake_config.MISSING_CREDENTIALS = []

        fake_database = types.ModuleType("database")
        fake_database.DATABASE_PATH = Path("inventory.db")
        fake_database.INVENTORY_VALUE_COLUMN = "STOCK"
        fake_database.PRODUCT_TABLE = "PRODUCT"
        fake_database.ensure_schema = lambda path: None
        fake_database.validate_product_schema = lambda path: None
        fake_database.get_product_schema_description = (
            lambda path=None: "Product table schema: PRODUCT (ID INTEGER, NAME TEXT, STOCK INTEGER)"
        )

        class _FakeAnalysisResult:
            def __init__(self, text):
                self.text = text
                self.status = "success"
                self.model_name = "fake-model"
                self.error = None

        fake_analytics = types.ModuleType("analytics")
        fake_analytics.generate_insights = lambda df: _FakeAnalysisResult("insights")
        fake_analytics.predict_stock_needs = lambda df: _FakeAnalysisResult("predictions")
        fake_analytics.categorize_product = (
            lambda df, name, description: _FakeAnalysisResult("category")
        )
        fake_analytics.generate_report = lambda df: _FakeAnalysisResult("report")

        fake_pandasai = types.ModuleType("pandasai")

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            def chat(self, payload):
                return payload

        fake_pandasai.Agent = FakeAgent

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "inventory.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE PRODUCT (
                        ID INTEGER PRIMARY KEY AUTOINCREMENT,
                        NAME TEXT,
                        CATEGORY TEXT,
                        PRICE REAL,
                        STOCK INTEGER
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO PRODUCT (NAME, CATEGORY, PRICE, STOCK) VALUES (?, ?, ?, ?)",
                    ("Widget", "Gadgets", 9.99, 12),
                )
                connection.commit()

            import utils as utils_module

            original_pandas = utils_module._pandas
            try:
                # Force the lightweight dataframe shim so a stubbed pandas module
                # cannot break pyarrow/pandas interop during app import.
                utils_module._pandas = None
                with patched_modules(
                    {
                        "streamlit": fake_streamlit,
                        "pandas": fake_pandas,
                        "config": fake_config,
                        "database": fake_database,
                        "analytics": fake_analytics,
                        "pandasai": fake_pandasai,
                    }
                ):
                    with temporary_working_directory(Path(tmpdir)):
                        sys.modules.pop("app", None)
                        app = importlib.import_module("app")
            finally:
                utils_module._pandas = original_pandas

            self.assertEqual(app.db_path, "inventory.db")


if __name__ == "__main__":
    unittest.main()
