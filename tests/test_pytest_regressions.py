from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

import database
import prompt
from guardrails import DestructiveActionApprovalRequired, SchemaChangeApprovalRequired


class FakeColumns(list):
    def tolist(self):
        return list(self)


class FakeFrame:
    def __init__(self, rows):
        self._rows = list(rows)
        self.columns = FakeColumns(list(self._rows[0].keys()) if self._rows else [])

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        if not self._rows:
            return
        for index, row in enumerate(self._rows):
            yield index, row


@pytest.fixture
def inventory_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "inventory.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
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
        connection.executemany(
            """
            INSERT INTO PRODUCT
            (NAME, CATEGORY, BRAND, PRICE, STOCK, SIZE, COLOR, WEIGHT, SPECIFICATIONS)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("Widget", "Gadgets", "Acme", 9.99, 12, "M", "Blue", 1.2, "Original widget"),
                ("Gizmo", "Gadgets", "Acme", 19.99, 3, "L", "Black", 1.8, "Second product"),
            ],
        )
        connection.commit()
    return db_path


@pytest.fixture
def excel_processing_module(monkeypatch):
    fake_streamlit = types.ModuleType("streamlit")
    fake_streamlit.write = lambda *args, **kwargs: None

    fake_pandas = types.ModuleType("pandas")
    fake_pandas.read_excel = lambda uploaded_file: None

    monkeypatch.setitem(sys.modules, "streamlit", fake_streamlit)
    monkeypatch.setitem(sys.modules, "pandas", fake_pandas)
    # Use monkeypatch so the entry is restored after the test, avoiding stale
    # cached modules that imported with fake streamlit/pandas dependencies.
    monkeypatch.delitem(sys.modules, "excel_processing", raising=False)

    module = importlib.import_module("excel_processing")
    # Register the freshly imported module through monkeypatch so teardown
    # removes it and downstream tests start clean.
    monkeypatch.setitem(sys.modules, "excel_processing", module)

    # Isolate the test from utils/prompt: map each Excel column name to its
    # upper-cased equivalent (matching the PRODUCT schema convention) without
    # calling Gemini or the heuristic logic.
    monkeypatch.setattr(module, "map_columns", lambda cols, existing, fn: {c: c.upper() for c in cols})

    return module


def test_ensure_schema_creates_product_table(tmp_path: Path):
    db_path = tmp_path / "schema_only.db"
    database.ensure_schema(db_path)
    database.validate_product_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM PRODUCT").fetchone()[0]

    assert row_count == 0


def test_ensure_schema_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "idempotent.db"
    database.ensure_schema(db_path)
    # Second call must not raise or duplicate data.
    database.ensure_schema(db_path)
    database.validate_product_schema(db_path)


def test_ensure_schema_migrates_legacy_quantity_column(tmp_path: Path):
    db_path = tmp_path / "legacy_quantity.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
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
        connection.execute(
            """
            INSERT INTO PRODUCT
            (NAME, CATEGORY, BRAND, PRICE, QUANTITY, SIZE, COLOR, WEIGHT, SPECIFICATIONS)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("Widget", "Gadgets", "Acme", 9.99, 12, "M", "Blue", 1.2, "Original widget"),
        )

    database.ensure_schema(db_path)
    database.validate_product_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(PRODUCT)").fetchall()]
        migrated_row = connection.execute(
            "SELECT NAME, PRICE, STOCK FROM PRODUCT"
        ).fetchone()
        schema_version = connection.execute(
            "SELECT MAX(version) FROM _schema_version"
        ).fetchone()[0]

    assert "QUANTITY" not in columns
    assert database.INVENTORY_VALUE_COLUMN in columns
    assert migrated_row == ("Widget", 9.99, 12)
    assert schema_version == 1


def test_ensure_schema_repairs_legacy_schema_even_with_stale_version_metadata(tmp_path: Path):
    db_path = tmp_path / "stale_version.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
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
        connection.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO _schema_version (version) VALUES (1)")

    database.ensure_schema(db_path)
    database.validate_product_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(PRODUCT)").fetchall()]

    assert "QUANTITY" not in columns
    assert database.INVENTORY_VALUE_COLUMN in columns


def test_seed_database_populates_rows(monkeypatch, tmp_path: Path):
    seeded_rows = [
        ("Widget", "Gadgets", "Acme", 9.99, 12, "M", "Blue", 1.2, "Original widget"),
        ("Cable", "Accessories", "Acme", 4.5, 20, "N/A", "White", 0.3, "Charging cable"),
    ]
    monkeypatch.setattr(database, "generate_product_data", lambda num_products: list(seeded_rows))

    db_path = tmp_path / "seeded_inventory.db"
    database.ensure_schema(db_path)
    database.seed_database(db_path, num_products=len(seeded_rows))
    database.validate_product_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM PRODUCT").fetchone()[0]
        total_value = connection.execute("SELECT SUM(PRICE * STOCK) FROM PRODUCT").fetchone()[0]

    assert row_count == len(seeded_rows)
    assert total_value == pytest.approx((9.99 * 12) + (4.5 * 20))


def test_seed_database_is_idempotent_by_default(monkeypatch, tmp_path: Path):
    seeded_rows = [
        ("Widget", "Gadgets", "Acme", 9.99, 12, "M", "Blue", 1.2, "Original widget"),
    ]
    monkeypatch.setattr(database, "generate_product_data", lambda num_products: list(seeded_rows))

    db_path = tmp_path / "idempotent_seed.db"
    database.ensure_schema(db_path)
    database.seed_database(db_path)
    # Second call without force=True must not insert duplicate rows.
    database.seed_database(db_path)

    with sqlite3.connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM PRODUCT").fetchone()[0]

    assert row_count == len(seeded_rows)


def test_initialize_database_only_applies_schema(tmp_path: Path):
    db_path = tmp_path / "schema_only.db"
    database.initialize_database(db_path)
    database.validate_product_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM PRODUCT").fetchone()[0]

    # initialize_database must not seed rows — seeding is a separate step.
    assert row_count == 0


def test_validate_product_schema_raises_when_table_is_absent(tmp_path: Path):
    # Covers the case where the database exists but has no PRODUCT table at all
    # (complementary to test_database_schema.py which always creates the table first).
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()

    with pytest.raises(RuntimeError, match="does not contain the required"):
        database.validate_product_schema(db_path)


def test_process_excel_file_modify_updates_existing_rows(
    excel_processing_module,
    inventory_db: Path,
):
    # fake_pandas is a fresh types.ModuleType created per test inside the fixture;
    # direct assignment needs no monkeypatch teardown because the object is discarded.
    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame(
        [
            {
                "Name": "Widget",
                "Category": "Premium Gadgets",
                "Price": 12.5,
                "Stock": 20,
                "Color": "Orange",
            }
        ]
    )

    excel_processing_module.process_excel_file(
        object(),
        str(inventory_db),
        "modify",
        allow_destructive_actions=True,
    )

    with sqlite3.connect(inventory_db) as connection:
        row = connection.execute(
            "SELECT NAME, CATEGORY, PRICE, STOCK, COLOR FROM PRODUCT WHERE NAME = ?",
            ("Widget",),
        ).fetchone()

    assert row == ("Widget", "Premium Gadgets", 12.5, 20, "Orange")


def test_process_excel_file_remove_deletes_matching_rows(
    excel_processing_module,
    inventory_db: Path,
):
    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame([{"Name": "Gizmo"}])

    excel_processing_module.process_excel_file(
        object(),
        str(inventory_db),
        "remove",
        allow_destructive_actions=True,
    )

    with sqlite3.connect(inventory_db) as connection:
        remaining_names = [row[0] for row in connection.execute("SELECT NAME FROM PRODUCT")]

    assert remaining_names == ["Widget"]


def test_process_excel_file_add_inserts_new_product(
    excel_processing_module,
    inventory_db: Path,
):
    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame(
        [{"Name": "NewWidget", "Category": "Tools", "Price": 5.0, "Stock": 10}]
    )

    excel_processing_module.process_excel_file(object(), str(inventory_db), "add")

    with sqlite3.connect(inventory_db) as connection:
        names = [row[0] for row in connection.execute("SELECT NAME FROM PRODUCT ORDER BY NAME")]

    assert "NewWidget" in names
    assert len(names) == 3  # Widget + Gizmo + NewWidget


def test_process_excel_file_add_upserts_existing_product(
    excel_processing_module,
    inventory_db: Path,
):
    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame(
        [{"Name": "Widget", "Category": "Updated Gadgets", "Price": 15.0, "Stock": 5}]
    )

    excel_processing_module.process_excel_file(object(), str(inventory_db), "add")

    with sqlite3.connect(inventory_db) as connection:
        row = connection.execute(
            "SELECT NAME, CATEGORY, PRICE, STOCK FROM PRODUCT WHERE NAME = ?",
            ("Widget",),
        ).fetchone()
        total_rows = connection.execute("SELECT COUNT(*) FROM PRODUCT").fetchone()[0]

    assert row == ("Widget", "Updated Gadgets", 15.0, 5)
    assert total_rows == 2  # no duplicate inserted


def test_process_excel_file_uses_reviewed_preview_without_regenerating_mapping(
    excel_processing_module,
    inventory_db: Path,
    monkeypatch,
):
    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame(
        [{"Name": "Widget", "Category": "Updated Gadgets"}]
    )

    preview = excel_processing_module.preview_excel_import(object(), str(inventory_db))
    assert preview["column_mappings"]["Category"] == "CATEGORY"

    def fail_if_mapping_recomputed(*args, **kwargs):
        raise AssertionError("process_excel_file should reuse the reviewed preview")

    monkeypatch.setattr(excel_processing_module, "map_columns", fail_if_mapping_recomputed)

    excel_processing_module.process_excel_file(
        object(),
        str(inventory_db),
        "add",
        preview=preview,
    )

    with sqlite3.connect(inventory_db) as connection:
        row = connection.execute(
            "SELECT NAME, CATEGORY, PRICE FROM PRODUCT WHERE NAME = ?",
            ("Widget",),
        ).fetchone()

    assert row == ("Widget", "Updated Gadgets", 9.99)


def test_process_excel_file_blocks_ai_schema_changes_without_approval(
    excel_processing_module,
    inventory_db: Path,
    monkeypatch,
):
    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame(
        [{"Name": "Widget", "Mystery Metric": "High"}]
    )
    monkeypatch.setattr(
        excel_processing_module,
        "map_columns",
        lambda cols, existing, fn: {"Name": "NAME", "Mystery Metric": "mystery metric"},
    )

    with pytest.raises(SchemaChangeApprovalRequired, match="MYSTERY_METRIC"):
        excel_processing_module.process_excel_file(object(), str(inventory_db), "add")

    with sqlite3.connect(inventory_db) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(PRODUCT)")]

    assert "MYSTERY_METRIC" not in columns
    audit_path = inventory_db.with_name("ai_operation_audit.jsonl")
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event_type"] == "excel_import_processed"
    assert events[-1]["details"]["status"] == "blocked"


def test_process_excel_file_blocks_destructive_actions_without_approval(
    excel_processing_module,
    inventory_db: Path,
):
    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame([{"Name": "Gizmo"}])

    with pytest.raises(DestructiveActionApprovalRequired, match="REMOVE"):
        excel_processing_module.process_excel_file(object(), str(inventory_db), "remove")

    with sqlite3.connect(inventory_db) as connection:
        remaining_names = [row[0] for row in connection.execute("SELECT NAME FROM PRODUCT ORDER BY NAME")]

    assert remaining_names == ["Gizmo", "Widget"]
    audit_path = inventory_db.with_name("ai_operation_audit.jsonl")
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event_type"] == "excel_import_processed"
    assert events[-1]["details"]["status"] == "blocked"
    assert events[-1]["details"]["error"].endswith("Blocked action: REMOVE.")


def test_process_excel_file_remove_by_id_with_duplicate_names(
    excel_processing_module,
    inventory_db: Path,
):
    with sqlite3.connect(inventory_db) as connection:
        connection.execute(
            """
            INSERT INTO PRODUCT
            (NAME, CATEGORY, BRAND, PRICE, STOCK, SIZE, COLOR, WEIGHT, SPECIFICATIONS)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("Widget", "Gadgets", "Acme", 1.0, 1, "S", "Red", 0.5, "Duplicate name"),
        )
        widget_ids = [
            row[0]
            for row in connection.execute(
                "SELECT ID FROM PRODUCT WHERE NAME = ? ORDER BY ID",
                ("Widget",),
            )
        ]
    target_id = widget_ids[0]

    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame(
        [{"Id": target_id, "Name": "Widget"}]
    )

    excel_processing_module.process_excel_file(
        object(),
        str(inventory_db),
        "remove",
        allow_destructive_actions=True,
    )

    with sqlite3.connect(inventory_db) as connection:
        remaining_widget_ids = [
            row[0]
            for row in connection.execute(
                "SELECT ID FROM PRODUCT WHERE NAME = ? ORDER BY ID",
                ("Widget",),
            )
        ]
        all_names = [row[0] for row in connection.execute("SELECT NAME FROM PRODUCT ORDER BY NAME")]

    assert target_id not in remaining_widget_ids
    assert len(remaining_widget_ids) == 1
    assert "Gizmo" in all_names


def test_process_excel_file_remove_rejects_ambiguous_name(
    excel_processing_module,
    inventory_db: Path,
):
    with sqlite3.connect(inventory_db) as connection:
        connection.execute(
            """
            INSERT INTO PRODUCT
            (NAME, CATEGORY, BRAND, PRICE, STOCK, SIZE, COLOR, WEIGHT, SPECIFICATIONS)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("Widget", "Gadgets", "Acme", 1.0, 1, "S", "Red", 0.5, "Duplicate name"),
        )

    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame([{"Name": "Widget"}])

    with pytest.raises(excel_processing_module.AmbiguousProductMatchError, match="Ambiguous remove"):
        excel_processing_module.process_excel_file(
            object(),
            str(inventory_db),
            "remove",
            allow_destructive_actions=True,
        )

    with sqlite3.connect(inventory_db) as connection:
        widget_count = connection.execute(
            "SELECT COUNT(*) FROM PRODUCT WHERE NAME = ?",
            ("Widget",),
        ).fetchone()[0]
    assert widget_count == 2


def test_process_excel_file_modify_by_id(
    excel_processing_module,
    inventory_db: Path,
):
    with sqlite3.connect(inventory_db) as connection:
        widget_id = connection.execute(
            "SELECT ID FROM PRODUCT WHERE NAME = ?",
            ("Widget",),
        ).fetchone()[0]

    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame(
        [{"Id": widget_id, "Name": "Widget", "Category": "ID Updated", "Stock": 99}]
    )

    excel_processing_module.process_excel_file(
        object(),
        str(inventory_db),
        "modify",
        allow_destructive_actions=True,
    )

    with sqlite3.connect(inventory_db) as connection:
        row = connection.execute(
            "SELECT ID, NAME, CATEGORY, STOCK FROM PRODUCT WHERE ID = ?",
            (widget_id,),
        ).fetchone()

    assert row == (widget_id, "Widget", "ID Updated", 99)


def test_estimate_import_impact_reports_ambiguous_names(
    excel_processing_module,
    inventory_db: Path,
):
    with sqlite3.connect(inventory_db) as connection:
        connection.execute(
            """
            INSERT INTO PRODUCT
            (NAME, CATEGORY, BRAND, PRICE, STOCK, SIZE, COLOR, WEIGHT, SPECIFICATIONS)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("Widget", "Gadgets", "Acme", 1.0, 1, "S", "Red", 0.5, "Duplicate name"),
        )

    excel_processing_module.pd.read_excel = lambda uploaded_file: FakeFrame([{"Name": "Widget"}])
    preview = excel_processing_module.preview_excel_import(object(), str(inventory_db))
    impact = excel_processing_module.estimate_import_impact(preview, str(inventory_db), "remove")

    assert impact["rows_in_file"] == 1
    assert impact["matched_db_rows"] == 2
    assert impact["ambiguous_names"] == ["Widget"]
    assert impact["identity_modes"] == ["NAME"]


def test_get_gemini_response_uses_mocked_sdk_when_api_key_is_available(monkeypatch):
    calls = []

    class FakeModel:
        def __init__(self, model_name):
            calls.append(("model", model_name))

        def generate_content(self, supplied_prompt):
            calls.append(("prompt", supplied_prompt))
            return types.SimpleNamespace(text="SELECT * FROM PRODUCT")

    fake_genai = types.SimpleNamespace(
        configure=lambda **kwargs: calls.append(("configure", kwargs)),
        GenerativeModel=FakeModel,
    )
    fake_google = types.ModuleType("google")
    fake_google.__path__ = []
    fake_google.generativeai = fake_genai

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)

    response = prompt.get_gemini_response("List all products", model_name="fake-model")

    assert response == "SELECT * FROM PRODUCT"
    assert ("configure", {"api_key": "test-key"}) in calls
    assert ("model", "fake-model") in calls
    assert ("prompt", "List all products") in calls


def test_get_gemini_response_falls_back_when_no_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    # SQL prompts intentionally return empty so generate_sql_query can fall back
    # using the original user question rather than the full prompt template.
    response = prompt.get_gemini_response("how many products are there")
    assert response == ""

    sql = prompt.generate_sql_query("PRODUCT schema", "how many products are there")
    assert sql == "SELECT COUNT(*) AS product_count FROM PRODUCT"
