"""Audit helpers for AI-assisted inventory operations."""

from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

AUDIT_LOG_FILENAME = "ai_operation_audit.jsonl"
AUDIT_LOG_WARN_BYTES = 100 * 1024 * 1024  # 100 MB
AUDIT_LOG_KEEP_ROTATIONS = 3


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(item) for item in value]
    return str(value)


def get_audit_log_path(db_path: str | Path) -> Path:
    """Store audit events alongside the active database."""

    return Path(db_path).resolve().with_name(AUDIT_LOG_FILENAME)


def _rotated_audit_path(audit_path: Path, index: int) -> Path:
    return audit_path.with_name(f"{audit_path.name}.{index}")


def rotate_audit_log_if_needed(audit_path: Path, *, max_bytes: int = AUDIT_LOG_WARN_BYTES) -> bool:
    """Rotate the audit log when it reaches ``max_bytes``.

    Keeps the newest ``AUDIT_LOG_KEEP_ROTATIONS`` archives as
    ``<name>.1`` … ``<name>.N`` (``.1`` is the most recent archive).
    Returns ``True`` when a rotation occurred.
    """

    if not audit_path.exists():
        return False

    file_size = audit_path.stat().st_size
    if file_size < max_bytes:
        return False

    # Drop the oldest archive, then shift .N-1 → .N … .1 → .2, current → .1.
    oldest = _rotated_audit_path(audit_path, AUDIT_LOG_KEEP_ROTATIONS)
    if oldest.exists():
        oldest.unlink()

    for index in range(AUDIT_LOG_KEEP_ROTATIONS - 1, 0, -1):
        source = _rotated_audit_path(audit_path, index)
        if source.exists():
            source.rename(_rotated_audit_path(audit_path, index + 1))

    audit_path.rename(_rotated_audit_path(audit_path, 1))

    warnings.warn(
        f"Audit log {audit_path} reached {file_size / (1024 * 1024):.1f} MB and was rotated. "
        f"Keeping the newest {AUDIT_LOG_KEEP_ROTATIONS} archives.",
        UserWarning,
        stacklevel=3,
    )
    return True


def append_audit_event(db_path: str | Path, event_type: str, details: Mapping[str, Any]) -> Path:
    """Append a structured audit event to the JSONL log.

    When the active log reaches ``AUDIT_LOG_WARN_BYTES`` (100 MB), it is rotated
    into numbered archives before the new event is written. A :class:`UserWarning`
    is emitted when rotation occurs.
    """

    audit_path = get_audit_log_path(db_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_audit_log_if_needed(audit_path)

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": str(event_type),
        "details": _to_json_safe(dict(details)),
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True))
        handle.write("\n")
    return audit_path
