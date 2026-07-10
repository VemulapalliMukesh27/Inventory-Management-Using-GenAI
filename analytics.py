"""
analytics.py

This module contains functions to analyze inventory data using Gemini to generate
insights, predict stock needs, categorize products, and generate comprehensive
reports. When Gemini is unavailable, deterministic inventory summaries are returned.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from typing import Any

from config import DEFAULT_GEMINI_MODEL, GOOGLE_API_KEY

DEFAULT_MODEL_NAME = DEFAULT_GEMINI_MODEL
_MAX_CONTEXT_ROWS = 10
_LOW_STOCK_THRESHOLD = 10
_AI_UNAVAILABLE_NOTE = (
    "Note: AI analysis was unavailable, so this summary was generated from "
    "inventory statistics only."
)

_BASE_ANALYSIS_INSTRUCTION = (
    "You are an expert in data analysis. Help non-technical people understand "
    "inventory data, trends, and operational risks."
)


def _load_generative_ai():
    """Load and validate the installed google-generativeai SDK surface."""
    try:
        import google.generativeai as genai
    except ImportError as exc:  # pragma: no cover - exercised via boundary tests
        raise RuntimeError(
            "google-generativeai is required for analytics features."
        ) from exc

    _validate_generative_ai_module(genai)
    return genai


def _validate_generative_ai_module(genai_module: Any) -> None:
    """Ensure the SDK surface exposes the model APIs this module depends on."""
    if not hasattr(genai_module, "configure") or not hasattr(genai_module, "GenerativeModel"):
        raise RuntimeError(
            "Installed google-generativeai SDK must expose configure() and GenerativeModel."
        )


def _extract_text(response: Any) -> str:
    """Normalize Gemini responses into plain text."""
    if isinstance(response, str):
        return response

    text = getattr(response, "text", None)
    if text:
        return text

    candidates = getattr(response, "candidates", None)
    if candidates:
        parts = []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            content_parts = getattr(content, "parts", None) if content else None
            if not content_parts:
                continue
            for part in content_parts:
                part_text = getattr(part, "text", None)
                if part_text:
                    parts.append(part_text)
        if parts:
            return "".join(parts)

    return str(response)


def _column_values(df: Any, *candidates: str) -> list[Any]:
    """Return values for the first matching column name (case-insensitive)."""
    columns = list(getattr(df, "columns", []))
    lookup = {str(column).upper(): column for column in columns}
    for candidate in candidates:
        key = candidate.upper()
        if key not in lookup:
            continue
        series = df[lookup[key]]
        values = getattr(series, "tolist", None)
        if callable(values):
            return list(values())
        if hasattr(series, "values"):
            return list(series.values)
        try:
            return list(series)
        except TypeError:
            return []
    return []


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    if number is None:
        return None
    return int(number)


def _build_deterministic_summary(
    df: Any,
    *,
    task: str,
    product_name: str | None = None,
    product_description: str | None = None,
) -> str:
    """Build a useful inventory summary without calling an LLM."""
    try:
        row_count = len(df)
    except TypeError:
        row_count = 0

    names = _column_values(df, "NAME")
    categories = _column_values(df, "CATEGORY")
    prices = [_safe_float(value) for value in _column_values(df, "PRICE")]
    stocks = [_safe_int(value) for value in _column_values(df, "STOCK", "QUANTITY")]

    valid_prices = [price for price in prices if price is not None]
    valid_stocks = [stock for stock in stocks if stock is not None]
    inventory_value = 0.0
    for price, stock in zip(prices, stocks):
        if price is not None and stock is not None:
            inventory_value += price * stock

    low_stock_items: list[str] = []
    for index, stock in enumerate(stocks):
        if stock is None or stock > _LOW_STOCK_THRESHOLD:
            continue
        label = names[index] if index < len(names) and names[index] is not None else f"row {index + 1}"
        low_stock_items.append(f"{label} (stock={stock})")

    category_counts = Counter(str(category) for category in categories if category is not None)
    top_categories = ", ".join(
        f"{name} ({count})" for name, count in category_counts.most_common(5)
    ) or "n/a"
    avg_price = sum(valid_prices) / len(valid_prices) if valid_prices else 0.0
    total_units = sum(valid_stocks) if valid_stocks else 0

    lines = [
        f"Deterministic {task} summary",
        f"- Products: {row_count}",
        f"- Total units in stock: {total_units}",
        f"- Total inventory value: ${inventory_value:,.2f}",
        f"- Average price: ${avg_price:,.2f}",
        f"- Top categories: {top_categories}",
        (
            f"- Low stock (≤{_LOW_STOCK_THRESHOLD}): "
            + (", ".join(low_stock_items[:10]) if low_stock_items else "none")
        ),
    ]

    if task == "stock prediction":
        if low_stock_items:
            lines.append(
                "- Replenishment priority: restock the low-stock items listed above first."
            )
        else:
            lines.append("- Replenishment priority: no products are currently at or below the threshold.")

    if task == "product categorization" and product_name is not None:
        guessed = "Uncategorized"
        if category_counts:
            guessed = category_counts.most_common(1)[0][0]
        description = (product_description or "").strip()
        lines.extend(
            [
                f"- Suggested category for '{product_name}': {guessed}",
                f"- Description provided: {description or '(none)'}",
                "- Suggestion is based on the most common existing category when AI is offline.",
            ]
        )

    lines.append(_AI_UNAVAILABLE_NOTE)
    return "\n".join(lines)


def _api_key_available() -> bool:
    return bool(GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY"))


def _build_inventory_context(df: Any) -> str:
    """Create a compact inventory snapshot for the language model."""
    columns = list(getattr(df, "columns", []))
    column_line = f"Columns: {', '.join(map(str, columns))}" if columns else "Columns: (unknown)"

    row_count = None
    try:
        row_count = len(df)
    except TypeError:
        row_count = None

    preview = str(df)
    head = getattr(df, "head", None)
    if callable(head):
        try:
            sample = head(_MAX_CONTEXT_ROWS)
            to_string = getattr(sample, "to_string", None)
            preview = to_string(index=False) if callable(to_string) else str(sample)
        except Exception:
            preview = str(df)

    row_line = f"Row count: {row_count}" if row_count is not None else "Row count: unknown"
    return f"{column_line}\n{row_line}\nSample rows:\n{preview}"


@dataclass
class GeminiAnalyticsClient:
    """Thin adapter around the supported Gemini SDK model abstraction."""

    model_name: str = DEFAULT_MODEL_NAME
    genai_module: Any | None = None

    def __post_init__(self) -> None:
        self._genai = self.genai_module or _load_generative_ai()
        _validate_generative_ai_module(self._genai)
        self._model = self._genai.GenerativeModel(self.model_name)

    def generate(self, prompt: str) -> str:
        response = self._model.generate_content(prompt)
        return _extract_text(response)


def _get_client() -> GeminiAnalyticsClient:
    return GeminiAnalyticsClient()


@dataclass
class AnalysisResult:
    """Structured analytics response with provenance for auditing."""

    text: str
    status: str  # success | fallback | failed
    model_name: str | None = None
    error: str | None = None


def _run_analysis(
    df: Any,
    task_instruction: str,
    task_prompt: str,
    *,
    task: str,
    client: GeminiAnalyticsClient | None = None,
    product_name: str | None = None,
    product_description: str | None = None,
) -> AnalysisResult:
    """Run Gemini analysis, falling back to deterministic stats when needed."""
    fallback = _build_deterministic_summary(
        df,
        task=task,
        product_name=product_name,
        product_description=product_description,
    )

    # An explicitly injected client (tests / custom adapters) bypasses the
    # API-key gate; production callers omit client and rely on GOOGLE_API_KEY.
    if client is None and not _api_key_available():
        return AnalysisResult(
            text=fallback,
            status="fallback",
            error="GOOGLE_API_KEY is not configured",
        )

    try:
        active_client = client or _get_client()
        context = _build_inventory_context(df)
        prompt = (
            f"{_BASE_ANALYSIS_INSTRUCTION}\n\n"
            f"{task_instruction}\n\n"
            f"{task_prompt}\n\n"
            f"Inventory context:\n{context}"
        )
        text = active_client.generate(prompt)
        return AnalysisResult(
            text=text,
            status="success",
            model_name=getattr(active_client, "model_name", DEFAULT_MODEL_NAME),
        )
    except Exception as exc:
        return AnalysisResult(
            text=fallback,
            status="fallback",
            model_name=DEFAULT_MODEL_NAME,
            error=str(exc),
        )


def generate_insights(df: Any, client: GeminiAnalyticsClient | None = None) -> AnalysisResult:
    """Generate key insights from the inventory data."""
    insights_prompt = (
        "Analyze this inventory data and provide key insights about stock levels, "
        "popular categories, and pricing trends."
    )
    return _run_analysis(
        df,
        "Focus on actionable inventory insights.",
        insights_prompt,
        task="inventory insights",
        client=client,
    )


def predict_stock_needs(df: Any, client: GeminiAnalyticsClient | None = None) -> AnalysisResult:
    """Predict which products are likely to run out of stock soon."""
    prediction_prompt = (
        "Based on the current inventory data, predict which products are likely to "
        "run out of stock in the next month. Consider historical sales data if available."
    )
    return _run_analysis(
        df,
        "Focus on stock risk and replenishment timing.",
        prediction_prompt,
        task="stock prediction",
        client=client,
    )


def categorize_product(
    df: Any,
    product_name: str,
    product_description: str,
    client: GeminiAnalyticsClient | None = None,
) -> AnalysisResult:
    """Categorize a product based on its name and description."""
    categorization_prompt = (
        f"Categorize this product:\nName: {product_name}\nDescription: {product_description}"
    )
    return _run_analysis(
        df,
        "Focus on the most appropriate inventory category.",
        categorization_prompt,
        task="product categorization",
        client=client,
        product_name=product_name,
        product_description=product_description,
    )


def generate_report(df: Any, client: GeminiAnalyticsClient | None = None) -> AnalysisResult:
    """Generate a comprehensive inventory report."""
    report_prompt = (
        "Generate a comprehensive inventory report. Include total inventory value, "
        "top-selling products, low stock alerts, and any notable trends."
    )
    return _run_analysis(
        df,
        "Produce a concise executive summary.",
        report_prompt,
        task="inventory report",
        client=client,
    )
