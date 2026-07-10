"""
excel_processing.py

This module handles the processing of uploaded Excel files and updates the database accordingly.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from audit import append_audit_event
from guardrails import (
    GuardrailViolation,
    enforce_destructive_action_policy,
    enforce_schema_change_policy,
    quote_identifier,
    review_column_mappings,
)
from prompt import get_column_mapping_prompt_metadata, get_gemini_response
from utils import _normalize_identifier, map_columns


class AmbiguousProductMatchError(ValueError):
    """Raised when a NAME-based remove/modify would touch more than one row."""


def _read_excel_frame(uploaded_file):
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    return pd.read_excel(uploaded_file)


def _mapped_row_from_excel_row(row: Any, column_mappings: dict[str, str]) -> dict[str, Any]:
    unmapped = [str(col) for col in row.keys() if str(col) not in column_mappings]
    if unmapped:
        raise ValueError(
            f"AI column mapping is incomplete — no mapping for: {', '.join(unmapped)}"
        )
    return {column_mappings[str(col)]: value for col, value in row.items()}


def _has_usable_id(mapped_row: dict[str, Any]) -> bool:
    if "ID" not in mapped_row:
        return False
    value = mapped_row.get("ID")
    if value is None:
        return False
    try:
        if isinstance(value, float) and value != value:  # NaN
            return False
    except Exception:
        pass
    text = str(value).strip()
    return bool(text) and text.lower() != "nan"


def _resolve_row_identity(mapped_row: dict[str, Any], action: str) -> tuple[str, Any]:
    """Return (mode, key) where mode is 'ID' or 'NAME'."""

    if _has_usable_id(mapped_row):
        return "ID", mapped_row["ID"]

    if "NAME" not in mapped_row or mapped_row.get("NAME") is None:
        raise ValueError(
            f"Row is missing an ID or NAME mapping; cannot determine which product to {action}."
        )
    name = mapped_row.get("NAME")
    if str(name).strip() == "" or str(name).strip().lower() == "nan":
        raise ValueError(
            f"Row is missing an ID or NAME mapping; cannot determine which product to {action}."
        )
    return "NAME", name


def _count_matches(cursor: sqlite3.Cursor, mode: str, key: Any) -> int:
    cursor.execute(
        f"SELECT COUNT(*) FROM PRODUCT WHERE {quote_identifier(mode)}=?",
        (key,),
    )
    return int(cursor.fetchone()[0])


def _enforce_unique_name_match(cursor: sqlite3.Cursor, mode: str, key: Any, action: str) -> None:
    """Fail closed when NAME matches multiple products for destructive actions."""

    if mode != "NAME" or action not in {"remove", "modify"}:
        return
    match_count = _count_matches(cursor, mode, key)
    if match_count > 1:
        raise AmbiguousProductMatchError(
            f"Ambiguous {action}: NAME={key!r} matches {match_count} products. "
            "Include an ID column to target a specific row."
        )


def estimate_import_impact(preview: dict[str, Any], db_path: str, action: str) -> dict[str, Any]:
    """Estimate how many DB rows an import action would touch."""

    df = preview["dataframe"]
    column_mappings = preview["column_mappings"]
    rows_in_file = len(df)
    matched_rows = 0
    ambiguous_names: list[str] = []
    identity_modes: set[str] = set()
    missing_identity = 0

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        for _, row in df.iterrows():
            try:
                mapped_row = _mapped_row_from_excel_row(row, column_mappings)
                mode, key = _resolve_row_identity(mapped_row, action)
            except ValueError:
                missing_identity += 1
                continue

            identity_modes.add(mode)
            match_count = _count_matches(cursor, mode, key)
            matched_rows += match_count
            if mode == "NAME" and match_count > 1 and action in {"remove", "modify"}:
                ambiguous_names.append(str(key))

    return {
        "action": action,
        "rows_in_file": rows_in_file,
        "matched_db_rows": matched_rows,
        "identity_modes": sorted(identity_modes),
        "ambiguous_names": sorted(set(ambiguous_names)),
        "missing_identity_rows": missing_identity,
    }


def preview_excel_import(uploaded_file, db_path, *, emit_audit_event=False):
    """Return the AI-produced column mapping and any pending schema changes."""

    df = _read_excel_frame(uploaded_file)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(PRODUCT)")
        existing_columns = [info[1] for info in cursor.fetchall()]

    column_mappings = map_columns(df.columns, existing_columns, get_gemini_response)
    review = review_column_mappings(column_mappings, existing_columns)
    preview = {
        "dataframe": df,
        "existing_columns": existing_columns,
        "column_mappings": review.sanitized_mapping,
        "proposed_new_columns": list(review.proposed_new_columns),
        "review": review,
    }
    if emit_audit_event:
        append_audit_event(
            db_path,
            "excel_import_preview",
            {
                **get_column_mapping_prompt_metadata(),
                "action": None,
                "uploaded_filename": getattr(uploaded_file, "name", None),
                "column_mappings": review.sanitized_mapping,
                "proposed_new_columns": list(review.proposed_new_columns),
            },
        )
    return preview


def process_excel_file(
    uploaded_file,
    db_path,
    action,
    allow_schema_changes=False,
    allow_destructive_actions=False,
    preview=None,
):
    """
    Processes an uploaded Excel file to update the PRODUCT table in the database.

    Args:
        uploaded_file: The uploaded Excel file.
        db_path (str): The path to the database.
        action (str): The action to perform ("add", "remove", or "modify").
    """
    preview = preview or preview_excel_import(uploaded_file, db_path)
    df = preview["dataframe"]
    column_mappings = preview["column_mappings"]
    audit_details = {
        **get_column_mapping_prompt_metadata(),
        "action": action,
        "uploaded_filename": getattr(uploaded_file, "name", None),
        "column_mappings": column_mappings,
        "proposed_new_columns": list(preview["proposed_new_columns"]),
        "allow_schema_changes": allow_schema_changes,
        "allow_destructive_actions": allow_destructive_actions,
    }
    processed_rows = 0
    affected_db_rows = 0

    try:
        enforce_destructive_action_policy(
            action,
            allow_destructive_actions=allow_destructive_actions,
        )
        enforce_schema_change_policy(
            preview["review"],
            allow_schema_changes=allow_schema_changes,
        )

        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # Add new columns within the same connection/transaction so that
            # a failure during row processing does not leave the database with
            # orphan columns added by a separate connection.
            existing = {_normalize_identifier(name): name for name in preview["existing_columns"]}
            for db_col in preview["proposed_new_columns"]:
                normalized = _normalize_identifier(db_col)
                if normalized not in existing:
                    col_type = "TEXT"
                    if normalized in {"ID", "STOCK", "QUANTITY", "COUNT"}:
                        col_type = "INTEGER"
                    elif normalized in {"PRICE", "WEIGHT", "COST", "AMOUNT"}:
                        col_type = "REAL"
                    cursor.execute(f'ALTER TABLE PRODUCT ADD COLUMN "{normalized}" {col_type}')
                    existing[normalized] = normalized

            # Process each row in the Excel file
            for _, row in df.iterrows():
                mapped_row = _mapped_row_from_excel_row(row, column_mappings)
                mode, key = _resolve_row_identity(mapped_row, action)
                _enforce_unique_name_match(cursor, mode, key, action)

                if action == "remove":
                    cursor.execute(
                        f"DELETE FROM PRODUCT WHERE {quote_identifier(mode)}=?",
                        (key,),
                    )
                    affected_db_rows += cursor.rowcount if cursor.rowcount is not None else 0
                elif action == "modify":
                    set_clause = ", ".join(
                        [f"{quote_identifier(col)}=?" for col in mapped_row.keys()]
                    )
                    values = tuple(mapped_row.values())
                    cursor.execute(
                        f"UPDATE PRODUCT SET {set_clause} WHERE {quote_identifier(mode)}=?",
                        values + (key,),
                    )
                    affected_db_rows += cursor.rowcount if cursor.rowcount is not None else 0
                else:  # add action (or update if product exists)
                    # Prefer ID when present; otherwise upsert by NAME.
                    cursor.execute(
                        f"SELECT * FROM PRODUCT WHERE {quote_identifier(mode)}=?",
                        (key,),
                    )
                    existing_product = cursor.fetchone()
                    if existing_product:
                        set_clause = ", ".join(
                            [f"{quote_identifier(col)}=?" for col in mapped_row.keys()]
                        )
                        values = tuple(mapped_row.values())
                        cursor.execute(
                            f"UPDATE PRODUCT SET {set_clause} WHERE {quote_identifier(mode)}=?",
                            values + (key,),
                        )
                    else:
                        insert_row = dict(mapped_row)
                        # Avoid inserting an explicit NULL/empty ID into an AUTOINCREMENT PK.
                        if mode == "NAME" and "ID" in insert_row and not _has_usable_id(insert_row):
                            insert_row.pop("ID", None)
                        columns = ", ".join(quote_identifier(col) for col in insert_row.keys())
                        placeholders = ", ".join(["?" for _ in insert_row])
                        values = tuple(insert_row.values())
                        cursor.execute(
                            f"INSERT INTO PRODUCT ({columns}) VALUES ({placeholders})",
                            values,
                        )
                    affected_db_rows += 1
                processed_rows += 1
        append_audit_event(
            db_path,
            "excel_import_processed",
            {
                **audit_details,
                "processed_rows": processed_rows,
                "affected_db_rows": affected_db_rows,
                "status": "success",
            },
        )
    except Exception as exc:
        append_audit_event(
            db_path,
            "excel_import_processed",
            {
                **audit_details,
                "processed_rows": processed_rows,
                "affected_db_rows": affected_db_rows,
                "status": "blocked" if isinstance(exc, GuardrailViolation) else "failed",
                "error": str(exc),
            },
        )
        raise
