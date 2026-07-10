from __future__ import annotations

import json
import warnings
from pathlib import Path

from audit import (
    AUDIT_LOG_KEEP_ROTATIONS,
    AUDIT_LOG_WARN_BYTES,
    append_audit_event,
    get_audit_log_path,
    rotate_audit_log_if_needed,
)
from prompt import (
    COLUMN_MAPPING_PROMPT_NAME,
    COLUMN_MAPPING_PROMPT_VERSION,
    SQL_GENERATION_PROMPT_NAME,
    SQL_GENERATION_PROMPT_VERSION,
    build_column_mapping_prompt,
    build_sql_generation_prompt,
)
from utils import map_columns


def test_append_audit_event_writes_jsonl_next_to_database(tmp_path: Path):
    db_path = tmp_path / "inventory.db"
    db_path.write_text("", encoding="utf-8")

    audit_path = append_audit_event(
        db_path,
        "sql_query_review",
        {"status": "executed", "prompt_version": "v1"},
    )

    assert audit_path == get_audit_log_path(db_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert payload["event_type"] == "sql_query_review"
    assert payload["details"]["status"] == "executed"
    assert payload["details"]["prompt_version"] == "v1"
    assert "timestamp" in payload


def test_build_sql_generation_prompt_contains_question_and_schema():
    # Prompt metadata (name/version) is recorded in the audit log, not sent to
    # the model. Verify that the prompt contains the actual content the model needs.
    prompt = build_sql_generation_prompt("PRODUCT(ID, NAME)", "List all products")

    assert "List all products" in prompt
    assert "PRODUCT(ID, NAME)" in prompt
    assert SQL_GENERATION_PROMPT_NAME  # constant must remain non-empty for audit use
    assert SQL_GENERATION_PROMPT_VERSION


def test_build_column_mapping_prompt_contains_columns():
    # Prompt metadata is in the audit log only, not the LLM prompt.
    prompt = build_column_mapping_prompt(["Name", "Price"], ["NAME", "PRICE"])

    assert "Name" in prompt
    assert "Price" in prompt
    assert "NAME" in prompt
    assert "PRICE" in prompt
    assert COLUMN_MAPPING_PROMPT_NAME  # constant must remain non-empty for audit use
    assert COLUMN_MAPPING_PROMPT_VERSION


def test_append_audit_event_rotates_when_log_exceeds_size_limit(tmp_path: Path):
    db_path = tmp_path / "inventory.db"
    db_path.write_text("", encoding="utf-8")
    audit_path = get_audit_log_path(db_path)
    archive_path = audit_path.with_name(f"{audit_path.name}.1")

    # Pre-create a log file that is exactly at the threshold.
    audit_path.write_bytes(b"x" * AUDIT_LOG_WARN_BYTES)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        append_audit_event(db_path, "test_event", {"key": "value"})

    assert len(caught) == 1
    assert issubclass(caught[0].category, UserWarning)
    assert "rotated" in str(caught[0].message).lower()
    assert archive_path.exists()
    assert archive_path.stat().st_size == AUDIT_LOG_WARN_BYTES
    # Fresh active log should contain only the newly appended event.
    payload = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert payload["event_type"] == "test_event"
    assert payload["details"]["key"] == "value"


def test_append_audit_event_no_warning_below_size_limit(tmp_path: Path):
    db_path = tmp_path / "inventory.db"
    db_path.write_text("", encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        append_audit_event(db_path, "test_event", {"key": "value"})

    assert len(caught) == 0


def test_rotate_audit_log_keeps_newest_archives(tmp_path: Path):
    audit_path = tmp_path / "ai_operation_audit.jsonl"
    audit_path.write_bytes(b"current")
    for index in range(1, AUDIT_LOG_KEEP_ROTATIONS + 1):
        audit_path.with_name(f"{audit_path.name}.{index}").write_text(
            f"archive-{index}",
            encoding="utf-8",
        )

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        rotated = rotate_audit_log_if_needed(audit_path, max_bytes=1)

    assert rotated is True
    assert not audit_path.exists()
    assert audit_path.with_name(f"{audit_path.name}.1").read_text(encoding="utf-8") == "current"
    assert audit_path.with_name(f"{audit_path.name}.2").read_text(encoding="utf-8") == "archive-1"
    assert audit_path.with_name(f"{audit_path.name}.3").read_text(encoding="utf-8") == "archive-2"
    assert not audit_path.with_name(f"{audit_path.name}.4").exists()


def test_rotate_audit_log_noop_below_threshold(tmp_path: Path):
    audit_path = tmp_path / "ai_operation_audit.jsonl"
    audit_path.write_text("small", encoding="utf-8")

    assert rotate_audit_log_if_needed(audit_path, max_bytes=1000) is False
    assert audit_path.read_text(encoding="utf-8") == "small"


def test_map_columns_uses_versioned_prompt_builder():
    captured = {}

    def fake_response(prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"Name": "NAME", "Price": "PRICE"}'

    mapping = map_columns(["Name", "Price"], ["NAME", "PRICE"], fake_response)

    assert mapping == {"Name": "NAME", "Price": "PRICE"}
    # The prompt must contain the columns so the model can produce a mapping.
    assert "Name" in captured["prompt"]
    assert "NAME" in captured["prompt"]


def test_heuristic_and_prompt_fallback_agree_on_aliases():
    from prompt import _fallback_column_mapping, build_column_mapping_prompt
    from utils import _heuristic_column_mapping

    excel_columns = ["Product Name", "Qty", "Colour", "Cost"]
    database_columns = ["ID", "NAME", "PRICE", "STOCK", "COLOR"]

    heuristic = _heuristic_column_mapping(excel_columns, database_columns)
    fallback = json.loads(
        _fallback_column_mapping(build_column_mapping_prompt(excel_columns, database_columns))
    )

    assert heuristic == fallback
    assert heuristic["Product Name"] == "NAME"
    assert heuristic["Qty"] == "STOCK"
    assert heuristic["Colour"] == "COLOR"
    assert heuristic["Cost"] == "PRICE"
