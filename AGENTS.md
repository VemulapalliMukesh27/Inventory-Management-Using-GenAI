# AGENTS.md

## Cursor Cloud specific instructions

This is a single Python/Streamlit product: an AI-powered Inventory Management System
(natural-language → SQL, dashboard, Excel upload, AI insights). Standard setup, lint,
test, and run commands are documented in `README.md` and `CLAUDE.md` — refer to those
rather than re-deriving them.

Environment notes for this VM:

- Python is `python3` (3.12); there is no `python3.11` binary. The project supports
  `>=3.11`, so 3.12 is fine (CI also tests 3.12). Use the `.venv` created by the update
  script; activate with `source .venv/bin/activate` or call binaries directly
  (e.g. `.venv/bin/streamlit`, `.venv/bin/pytest`, `.venv/bin/ruff`).
- The update script already installs dependencies into `.venv`. Do not reinstall unless
  `pyproject.toml`/`constraints.txt` changed.

Non-obvious startup caveats:

- **Database is not committed and not auto-seeded.** `product_inventory.db` is
  gitignored. `python database.py` only creates/migrates the schema (0 rows). To get
  demo data you MUST pass `--seed` (e.g. `python database.py --seed --seed-count 500`).
  The app calls `validate_product_schema()` at startup and `st.stop()`s if the `PRODUCT`
  table is missing, so run at least `python database.py` before launching the app.
  (Note: `CLAUDE.md`/`README.md` say `python database.py` seeds 10,000 rows — that is
  outdated; seeding is now opt-in via `--seed`.)
- **Runs fully without API keys.** `GOOGLE_API_KEY` / `PANDASAI_API_KEY` are optional.
  Without them the app shows a warning banner and natural-language → SQL falls back to
  deterministic keyword patterns in `prompt.py` (`_fallback_sql`); the AI analytics
  buttons (Insights/Predict/Categorize/Report) still degrade gracefully. Basic querying
  works out of the box. Add keys to a `.env` file for full LLM-backed features.
- Run the app with `streamlit run app.py`; for headless/background use add
  `--server.headless true --server.port 8501`. Health check: `curl localhost:8501/_stcore/health`.
- `pandasai` is intentionally not installed (pins `pandas==1.5.3`, incompatible with
  `pandas>=2.1`); the "Plot Parameters" feature warns and is skipped.
