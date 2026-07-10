"""Prompt helpers for SQL generation and Gemini-backed fallbacks.

The application imports this module at startup, so the functions here must be
safe even when optional AI dependencies or API keys are unavailable.
"""

from __future__ import annotations

import json
import os
import re

from config import DEFAULT_GEMINI_MODEL

_DEFAULT_SQL_LIMIT = 100
SQL_GENERATION_PROMPT_NAME = "sql_generation"
SQL_GENERATION_PROMPT_VERSION = "v1"
COLUMN_MAPPING_PROMPT_NAME = "column_mapping"
COLUMN_MAPPING_PROMPT_VERSION = "v1"


def _fallback_sql(question: str) -> str:
    text = question.lower().strip()

    if any(keyword in text for keyword in ("inventory value", "total value", "total worth", "worth")):
        return "SELECT COUNT(*) AS product_count, SUM(PRICE * STOCK) AS total_inventory_value FROM PRODUCT"

    if "how many" in text or "count" in text or "number of products" in text:
        return "SELECT COUNT(*) AS product_count FROM PRODUCT"

    if "low stock" in text or "out of stock" in text:
        return "SELECT * FROM PRODUCT WHERE STOCK <= 10 ORDER BY STOCK ASC"

    if "category" in text and "count" in text:
        return "SELECT CATEGORY, COUNT(*) AS product_count FROM PRODUCT GROUP BY CATEGORY ORDER BY product_count DESC"

    if "average price" in text or "avg price" in text:
        return "SELECT AVG(PRICE) AS average_price FROM PRODUCT"

    if "top" in text and "price" in text:
        return "SELECT * FROM PRODUCT ORDER BY PRICE DESC LIMIT 10"

    return f"SELECT * FROM PRODUCT LIMIT {_DEFAULT_SQL_LIMIT}"


def _fallback_column_mapping(prompt: str) -> str:
    excel_columns = []
    database_columns = []

    excel_match = re.search(r"Excel columns:\s*(.*)", prompt, re.IGNORECASE)
    database_match = re.search(r"Database columns:\s*(.*)", prompt, re.IGNORECASE)
    if excel_match:
        excel_columns = [value.strip() for value in excel_match.group(1).split(",") if value.strip()]
    if database_match:
        database_columns = [value.strip() for value in database_match.group(1).split(",") if value.strip()]

    # Lazy import avoids a circular dependency (utils imports prompt builders).
    from utils import _heuristic_column_mapping

    return json.dumps(_heuristic_column_mapping(excel_columns, database_columns))


def get_sql_prompt_metadata() -> dict[str, str]:
    return {
        "prompt_name": SQL_GENERATION_PROMPT_NAME,
        "prompt_version": SQL_GENERATION_PROMPT_VERSION,
    }


def get_column_mapping_prompt_metadata() -> dict[str, str]:
    return {
        "prompt_name": COLUMN_MAPPING_PROMPT_NAME,
        "prompt_version": COLUMN_MAPPING_PROMPT_VERSION,
    }


def build_sql_generation_prompt(db_description: str, question: str) -> str:
    """Build the versioned SQL-generation prompt."""

    return (
        "You are an expert SQL assistant. Generate a single SQL query only.\n"
        f"Database description: {db_description}\n"
        f"Question: {question}\n"
        "Return only the SQL statement."
    )


def build_column_mapping_prompt(excel_columns: list[str], existing_columns: list[str]) -> str:
    """Build the versioned Excel-column mapping prompt."""

    return (
        "Map these Excel columns to the database columns.\n"
        f"Excel columns: {', '.join(excel_columns)}\n"
        f"Database columns: {', '.join(existing_columns)}\n"
        "Return only a JSON object or simple key/value mapping."
    )


def _extract_sql(response: str) -> str:
    """Pull a SQL statement out of a model response, including markdown fences."""

    candidate = (response or "").strip()
    if not candidate:
        return ""

    fence_match = re.search(r"```(?:sql)?\s*([\s\S]*?)```", candidate, re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()

    candidate = candidate.strip().strip("`").strip()

    upper = candidate.upper()
    if upper.startswith("SELECT") or upper.startswith("WITH"):
        return candidate.rstrip(";").strip()

    # Model sometimes wraps SQL in a short prose preface.
    for match in re.finditer(r"\b(SELECT|WITH)\b[\s\S]*", candidate, re.IGNORECASE):
        snippet = match.group(0).strip().rstrip(";").strip()
        snippet_upper = snippet.upper()
        if snippet_upper.startswith("SELECT") or snippet_upper.startswith("WITH"):
            return snippet

    return ""


def get_gemini_response(prompt: str, model_name: str | None = None) -> str:
    """Return a Gemini response when available, otherwise a deterministic fallback."""

    resolved_model = model_name or DEFAULT_GEMINI_MODEL
    api_key = os.getenv("GOOGLE_API_KEY")
    if api_key:
        try:
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(resolved_model)
            response = model.generate_content(prompt)
            text = getattr(response, "text", None)
            if text:
                return text.strip()
        except Exception:
            pass

    if "excel columns" in prompt.lower() and "database columns" in prompt.lower():
        return _fallback_column_mapping(prompt)

    # For SQL prompts, return empty so callers can fall back using the original question
    # rather than keyword-matching against the full prompt template.
    return ""


def generate_sql_query(db_description: str, question: str) -> str:
    """Generate a SQL query for the question, using Gemini when available."""

    prompt = build_sql_generation_prompt(db_description, question)

    response = get_gemini_response(prompt)
    sql = _extract_sql(response)

    if sql.upper().startswith("SELECT") or sql.upper().startswith("WITH"):
        return sql

    return _fallback_sql(question)
