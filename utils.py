"""SQLite and Excel helper utilities used by the Streamlit app.

These helpers are intentionally dependency-light so the application can import
cleanly even when optional data-science packages are missing in a fresh
environment.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, Sequence

from prompt import build_column_mapping_prompt

try:  # Optional dependency for richer return values when available.
    import pandas as _pandas  # type: ignore
except Exception:  # pragma: no cover - exercised only when pandas is installed.
    _pandas = None


class _MiniSeries:
    def __init__(self, values: Sequence[object]):
        self.values = list(values)

    def tolist(self) -> list[object]:
        return list(self.values)


class _MiniColumns(list):
    def tolist(self) -> list[str]:
        return list(self)


class _MiniDataFrame:
    def __init__(self, rows: Sequence[dict[str, object]], columns: Sequence[str] | None = None):
        self._rows = [dict(row) for row in rows]
        if columns is None:
            column_names = []
            for row in self._rows:
                for column in row:
                    if column not in column_names:
                        column_names.append(column)
        else:
            column_names = list(columns)
        self.columns = _MiniColumns(column_names)

    @property
    def empty(self) -> bool:
        return len(self._rows) == 0

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, key):
        if isinstance(key, str):
            return _MiniSeries([row.get(key) for row in self._rows])
        if isinstance(key, (list, tuple)):
            rows = [{column: row.get(column) for column in key} for row in self._rows]
            return _MiniDataFrame(rows, key)
        raise TypeError(f"Unsupported key type: {type(key)!r}")

    def iterrows(self):
        for index, row in enumerate(self._rows):
            yield index, dict(row)

    def to_dict(self, orient: str = "records"):
        if orient != "records":
            raise ValueError("MiniDataFrame only supports orient='records'")
        return [dict(row) for row in self._rows]


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _normalize_alias_token(value: str) -> str:
    """Alphanumeric-only token used for synonym matching (e.g. Product Name → PRODUCTNAME)."""

    return re.sub(r"[^A-Z0-9]+", "", value.upper())


COLUMN_MAPPING_SYNONYMS: dict[str, tuple[str, ...]] = {
    "NAME": ("NAME", "PRODUCTNAME", "ITEMNAME", "PRODUCT"),
    "CATEGORY": ("CATEGORY", "TYPE", "GROUP"),
    "BRAND": ("BRAND",),
    "PRICE": ("PRICE", "COST", "AMOUNT", "RATE"),
    "STOCK": ("STOCK", "QUANTITY", "QTY", "INVENTORY"),
    "QUANTITY": ("QUANTITY", "STOCK", "QTY", "INVENTORY"),
    "COLOR": ("COLOR", "COLOUR"),
    "SIZE": ("SIZE",),
    "WEIGHT": ("WEIGHT",),
    "SPECIFICATIONS": ("SPECIFICATIONS", "SPECIFICATION", "DETAILS", "DESCRIPTION", "SPEC"),
    "ID": ("ID", "PRODUCTID"),
}


def _existing_columns(db_path: str) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        cursor = connection.cursor()
        cursor.execute("PRAGMA table_info(PRODUCT)")
        return [info[1] for info in cursor.fetchall()]


def _resolve_db_path(db_path: str) -> str:
    candidate = Path(db_path)
    if candidate.exists():
        return str(candidate)

    fallback = Path("product_inventory.db")
    if fallback.exists():
        return str(fallback)

    return str(candidate)


def _rewrite_query_for_known_schema(query: str) -> str:
    # The app still issues "quantity" queries even though the checked-in schema
    # uses STOCK. Normalizing that alias keeps the dashboard working.
    return re.sub(r"\bquantity\b", "STOCK", query, flags=re.IGNORECASE)


def _to_dataframe(rows: Sequence[dict[str, object]], columns: Sequence[str]):
    if _pandas is not None and hasattr(_pandas, "DataFrame"):  # pragma: no branch - runtime selection.
        return _pandas.DataFrame(list(rows), columns=list(columns))
    return _MiniDataFrame(rows, columns)


def read_sql_query(query: str, db_path: str):
    """Execute a SQL query against the inventory database."""

    resolved_db_path = _resolve_db_path(db_path)
    sql = _rewrite_query_for_known_schema(query)

    with sqlite3.connect(resolved_db_path) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            if sql != query:
                cursor.execute(_rewrite_query_for_known_schema(query))
            else:
                raise

        if cursor.description is None:
            return _to_dataframe([], [])

        columns = [description[0] for description in cursor.description]
        rows = [dict(row) for row in cursor.fetchall()]
        return _to_dataframe(rows, columns)


def _guess_sqlite_type(column_name: str) -> str:
    normalized = _normalize_identifier(column_name)
    if normalized in {"ID", "STOCK", "QUANTITY", "COUNT"}:
        return "INTEGER"
    if normalized in {"PRICE", "WEIGHT", "COST", "AMOUNT"}:
        return "REAL"
    return "TEXT"


def add_column_to_db(db_path: str, column_name: str, column_type: str | None = None) -> bool:
    """Add a column to PRODUCT if it does not already exist."""

    normalized_name = _normalize_identifier(column_name)
    if not normalized_name:
        raise ValueError("column_name must not be empty")

    resolved_db_path = _resolve_db_path(db_path)
    existing = {_normalize_identifier(name): name for name in _existing_columns(resolved_db_path)}
    if normalized_name in existing:
        return False

    chosen_type = column_type or _guess_sqlite_type(normalized_name)
    with sqlite3.connect(resolved_db_path) as connection:
        connection.execute(f'ALTER TABLE PRODUCT ADD COLUMN "{normalized_name}" {chosen_type}')
        connection.commit()
    return True


def _parse_mapping_response(response: str) -> dict[str, str]:
    response = response.strip()
    if not response:
        return {}

    try:
        data = json.loads(response)
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}
    except json.JSONDecodeError:
        pass

    mapping: dict[str, str] = {}
    for line in response.splitlines():
        if "->" in line:
            left, right = line.split("->", 1)
        elif ":" in line:
            left, right = line.split(":", 1)
        else:
            continue
        left = left.strip().strip("-* ")
        right = right.strip().strip("-* ,")
        if left and right:
            mapping[left] = right
    return mapping


def _heuristic_column_mapping(
    excel_columns: Iterable[str],
    existing_columns: Iterable[str],
) -> dict[str, str]:
    existing_lookup = {_normalize_identifier(column): column for column in existing_columns}
    result: dict[str, str] = {}

    for excel_column in excel_columns:
        normalized = _normalize_identifier(str(excel_column))
        alias_token = _normalize_alias_token(str(excel_column))
        if normalized in existing_lookup:
            result[str(excel_column)] = existing_lookup[normalized]
            continue

        mapped_column = None
        for target, aliases in COLUMN_MAPPING_SYNONYMS.items():
            if (
                normalized == target
                or normalized in aliases
                or alias_token == target
                or alias_token in aliases
            ):
                mapped_column = existing_lookup.get(_normalize_identifier(target), target)
                break

        if mapped_column is None:
            mapped_column = normalized or "COLUMN"

        result[str(excel_column)] = mapped_column

    return result


def map_columns(
    excel_columns: Iterable[str],
    existing_columns: Iterable[str],
    response_fn: Callable[[str], str] | None,
) -> dict[str, str]:
    """Map Excel column names to the PRODUCT table columns."""

    excel_columns = [str(column) for column in excel_columns]
    existing_columns = [str(column) for column in existing_columns]
    prompt = build_column_mapping_prompt(excel_columns, existing_columns)

    if response_fn is not None:
        try:
            response_mapping = _parse_mapping_response(response_fn(prompt))
            if response_mapping:
                normalized_existing = {_normalize_identifier(column): column for column in existing_columns}
                mapped: dict[str, str] = {}
                for excel_column in excel_columns:
                    raw_value = response_mapping.get(excel_column)
                    if raw_value is None:
                        raw_value = response_mapping.get(_normalize_identifier(excel_column))
                    if raw_value is None:
                        continue
                    normalized_raw = _normalize_identifier(str(raw_value))
                    mapped[excel_column] = normalized_existing.get(normalized_raw, normalized_raw)
                if len(mapped) == len(excel_columns):
                    return mapped
        except Exception:
            pass

    return _heuristic_column_mapping(excel_columns, existing_columns)
