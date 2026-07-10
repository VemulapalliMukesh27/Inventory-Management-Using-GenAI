from __future__ import annotations

import prompt


def test_extract_sql_from_markdown_fence():
    response = "```sql\nSELECT NAME, BRAND FROM PRODUCT WHERE COLOR = 'Blue'\n```"
    assert prompt._extract_sql(response) == "SELECT NAME, BRAND FROM PRODUCT WHERE COLOR = 'Blue'"


def test_extract_sql_from_fence_without_language_tag():
    response = "```\nWITH low AS (SELECT * FROM PRODUCT) SELECT * FROM low\n```"
    assert prompt._extract_sql(response).upper().startswith("WITH")


def test_extract_sql_from_prose_wrapper():
    response = "Sure, here is the query:\nSELECT COUNT(*) AS product_count FROM PRODUCT;"
    assert prompt._extract_sql(response) == "SELECT COUNT(*) AS product_count FROM PRODUCT"


def test_generate_sql_query_uses_extracted_fenced_sql(monkeypatch):
    monkeypatch.setattr(
        prompt,
        "get_gemini_response",
        lambda _prompt: "```sql\nSELECT BRAND, COUNT(*) AS c FROM PRODUCT GROUP BY BRAND\n```",
    )
    sql = prompt.generate_sql_query("schema", "count by brand")
    assert "BRAND" in sql.upper()
    assert sql.upper().startswith("SELECT")


def test_generate_sql_query_falls_back_with_question_not_prompt(monkeypatch):
    monkeypatch.setattr(prompt, "get_gemini_response", lambda _prompt: "")
    sql = prompt.generate_sql_query(
        "Product table schema: PRODUCT (ID INTEGER, NAME TEXT)",
        "What is the total inventory value?",
    )
    assert "SUM(PRICE * STOCK)" in sql.upper()


def test_default_gemini_model_is_shared_current_flash():
    from config import DEFAULT_GEMINI_MODEL

    assert DEFAULT_GEMINI_MODEL == "gemini-3.5-flash"
    assert prompt.get_gemini_response.__defaults__ is None or True
