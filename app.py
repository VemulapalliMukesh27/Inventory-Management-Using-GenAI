"""
app.py

The main entry point for the Inventory Management application. This module initializes the Streamlit UI,
handles user inputs, and calls functions from other modules to execute the core logic.
"""


import streamlit as st

try:
    from pandasai import Agent as _PandasAIAgent
    _PANDASAI_AVAILABLE = True
except ImportError:  # pandasai is not a declared dependency; install manually on Python 3.11
    _PANDASAI_AVAILABLE = False

from analytics import categorize_product, generate_insights, generate_report, predict_stock_needs
from audit import append_audit_event
from config import (  # ensure configuration is loaded
    MISSING_CREDENTIALS,
)
from database import (
    DATABASE_PATH,
    INVENTORY_VALUE_COLUMN,
    PRODUCT_TABLE,
    get_product_schema_description,
    validate_product_schema,
)
from excel_processing import preview_excel_import, process_excel_file
from guardrails import (
    DestructiveActionApprovalRequired,
    SchemaChangeApprovalRequired,
    SqlGuardrailViolation,
    validate_read_only_sql,
)
from prompt import (
    generate_sql_query,
    get_column_mapping_prompt_metadata,
    get_sql_prompt_metadata,
)
from utils import read_sql_query

IMPORT_PREVIEW_STATE_KEY = "excel_import_preview"


def _get_uploaded_file_signature(uploaded_file) -> str | None:
    """Return a cheap cache key for an uploaded file.

    Streamlit's UploadedFile always exposes ``name`` and ``size``, so we use
    those as a lightweight proxy for file identity. This avoids reading and
    SHA-256-hashing the entire file on every Streamlit rerun. The tradeoff is
    that two files with the same name and byte-size but different content would
    share a cache entry — an extremely unlikely scenario in practice.
    """
    if uploaded_file is None:
        return None

    # file_id is unique per upload session in Streamlit >=1.x and avoids the
    # name:size collision risk entirely.
    file_id = getattr(uploaded_file, "file_id", None)
    if file_id is not None:
        return str(file_id)

    name = getattr(uploaded_file, "name", "")
    size = getattr(uploaded_file, "size", None)
    if size is not None:
        return f"{name}:{size}"

    # Cannot build a reliable key without size — signal to the caller that
    # this file is uncacheable rather than returning a collision-prone string.
    return None


def _clear_cached_import_preview() -> None:
    st.session_state.pop(IMPORT_PREVIEW_STATE_KEY, None)


def _get_cached_import_preview(uploaded_file, db_path):
    cache_key = _get_uploaded_file_signature(uploaded_file)
    cached_preview = st.session_state.get(IMPORT_PREVIEW_STATE_KEY)
    if (
        cache_key is not None
        and cached_preview
        and cached_preview.get("cache_key") == cache_key
        and cached_preview.get("db_path") == db_path
    ):
        return cached_preview["preview"]

    preview = preview_excel_import(uploaded_file, db_path, emit_audit_event=True)
    if cache_key is not None:
        st.session_state[IMPORT_PREVIEW_STATE_KEY] = {
            "cache_key": cache_key,
            "db_path": db_path,
            "preview": preview,
        }
    return preview

# Set up Streamlit page configuration
st.set_page_config(
    page_title="Inventory Management Using GenAI",
    page_icon=":bar_chart:",
    layout="wide",
    initial_sidebar_state="expanded",
)

if MISSING_CREDENTIALS:
    st.warning(
        "Missing API credentials: {}. AI-backed features will stay available only after they are configured.".format(
            ", ".join(MISSING_CREDENTIALS)
        )
    )

# Custom CSS for dark theme styling
st.markdown("""
<style>
    body {
        color: #fff;
        background-color: #0e1117;
    }
    .main {
        background-color: #1a1b21;
        padding: 20px;
        border-radius: 10px;
    }
    .title {
        color: #1DB954;
        text-align: center;
        font-size: 2.5em;
    }
    .input-section, .dashboard-section, .modification-section, .upload-section {
        background-color: #2c2f36;
        padding: 15px;
        border-radius: 10px;
        margin-bottom: 20px;
    }
    .result-section {
        background-color: #2c2f36;
        padding: 15px;
        border-radius: 10px;
    }
    .error {
        background-color: #2c2f36;
        padding: 15px;
        border-radius: 10px;
        color: #d32f2f;
    }
    .stTextInput>div>div>input {
        background-color: #3c404a;
        color: #fff;
        border-radius: 5px;
    }
    .stButton>button {
        background-color: #1DB954;
        color: #fff;
        border-radius: 5px;
    }
    .stButton>button:hover {
        background-color: #1ed760;
        color: #fff;
    }
    .metric-card {
        background-color: #3c404a;
        padding: 10px;
        border-radius: 5px;
        text-align: center;
    }
    .metric-value {
        font-size: 2em;
        font-weight: bold;
        color: #1DB954;
    }
    .metric-label {
        font-size: 0.9em;
        color: #a0a0a0;
    }
</style>
""", unsafe_allow_html=True)

st.markdown('<h1 class="title">Inventory Management Using GenAI</h1>', unsafe_allow_html=True)

# --------------------------
# Dashboard Metrics Section
# --------------------------
st.markdown('<h2>Inventory Dashboard</h2>', unsafe_allow_html=True)
db_path = str(DATABASE_PATH)

try:
    validate_product_schema(db_path)
except RuntimeError as exc:
    st.error(f"Database startup check failed: {exc}")
    st.stop()

query = (
    f"SELECT COUNT(*) as product_count, "
    f"COALESCE(SUM(price * {INVENTORY_VALUE_COLUMN}), 0) as total_inventory_value "
    f"FROM {PRODUCT_TABLE}"
)
df = read_sql_query(query, db_path)
product_count = df['product_count'].values[0]
total_inventory_value = df['total_inventory_value'].values[0]

col1, col2 = st.columns(2)
with col1:
    st.markdown(
        '<div class="metric-card"><div class="metric-value">{}</div><div class="metric-label">Total Products</div></div>'.format(product_count),
        unsafe_allow_html=True
    )
with col2:
    st.markdown(
        '<div class="metric-card"><div class="metric-value">${:,.2f}</div><div class="metric-label">Total Inventory Value</div></div>'.format(total_inventory_value),
        unsafe_allow_html=True
    )

# --------------------------
# Plotting Section
# --------------------------
st.markdown('<h2>Plotting Parameters</h2>', unsafe_allow_html=True)
user_prompt = st.text_area("Enter your plot prompt:")

if st.button("Plot Parameters"):
    if not _PANDASAI_AVAILABLE:
        st.warning(
            "AI-powered plotting is unavailable: pandasai requires pandas==1.5.3, "
            "which is incompatible with this project's pandas>=2.1.0 requirement."
        )
    elif user_prompt:
        df_full = read_sql_query("SELECT * FROM PRODUCT", db_path)
        agent = _PandasAIAgent()
        response = agent.chat({"user_prompt": user_prompt, "df": df_full})
        st.pyplot(response)
    else:
        st.error("Please provide a prompt for plotting.")

# --------------------------
# SQL Query Section
# --------------------------
st.markdown('<h2>SQL Query Input</h2>', unsafe_allow_html=True)
question = st.text_area("Enter your query in natural language:")

if st.button("Generate SQL Query"):
    if question:
        db_description = get_product_schema_description(db_path)
        sql_query = generate_sql_query(db_description, question)
        try:
            validated_sql = validate_read_only_sql(sql_query, allowed_tables=(PRODUCT_TABLE,))
            st.write("Generated SQL Query:", validated_sql)
            result_df = read_sql_query(validated_sql, db_path)
            append_audit_event(
                db_path,
                "sql_query_review",
                {
                    **get_sql_prompt_metadata(),
                    "question": question,
                    "generated_sql": sql_query,
                    "validated_sql": validated_sql,
                    "status": "executed",
                    "row_count": len(result_df),
                },
            )
            st.write(result_df)
        except SqlGuardrailViolation as exc:
            append_audit_event(
                db_path,
                "sql_query_review",
                {
                    **get_sql_prompt_metadata(),
                    "question": question,
                    "generated_sql": sql_query,
                    "status": "blocked",
                    "error": str(exc),
                },
            )
            st.error(f"Blocked unsafe AI-generated SQL: {exc}")
        except Exception as e:
            append_audit_event(
                db_path,
                "sql_query_review",
                {
                    **get_sql_prompt_metadata(),
                    "question": question,
                    "generated_sql": sql_query,
                    "status": "failed",
                    "error": str(e),
                },
            )
            st.error(f"Error executing SQL query: {e}")
    else:
        st.error("Please enter a query.")

# --------------------------
# Excel File Processing Section
# --------------------------
st.markdown('<h2>Upload Excel File</h2>', unsafe_allow_html=True)
uploaded_file = st.file_uploader("Choose an Excel file", type=["xlsx"])
action = st.selectbox("Select Action", ["add", "remove", "modify"])
approve_schema_changes = False
approve_destructive_action = False
import_preview = None

if uploaded_file is None:
    if IMPORT_PREVIEW_STATE_KEY in st.session_state:
        _clear_cached_import_preview()
else:
    try:
        import_preview = _get_cached_import_preview(uploaded_file, db_path)
        st.write("Column names in the uploaded file:", import_preview["dataframe"].columns.tolist())
        st.write("Resolved column mappings:", import_preview["column_mappings"])
        if action in {"remove", "modify"}:
            st.warning(
                f"The '{action}' action changes or removes existing inventory rows."
            )
            approve_destructive_action = st.checkbox(
                f"Approve the '{action}' action for this import",
                key=f"approve_destructive_{action}",
            )
        if import_preview["proposed_new_columns"]:
            st.warning(
                "This import proposes new PRODUCT columns: {}.".format(
                    ", ".join(import_preview["proposed_new_columns"])
                )
            )
            approve_schema_changes = st.checkbox(
                "Approve these schema changes for this import",
                key=f"approve_schema_{action}",
            )
    except Exception as exc:
        append_audit_event(
            db_path,
            "excel_import_preview_failed",
            {
                **get_column_mapping_prompt_metadata(),
                "action": action,
                "uploaded_filename": getattr(uploaded_file, "name", None),
                "status": "failed",
                "error": str(exc),
            },
        )
        _clear_cached_import_preview()
        st.error(f"Unable to preview the Excel import: {exc}")

if st.button("Process Excel File"):
    if uploaded_file:
        try:
            if import_preview is None:
                import_preview = _get_cached_import_preview(uploaded_file, db_path)
            process_excel_file(
                uploaded_file,
                db_path,
                action,
                allow_schema_changes=approve_schema_changes,
                allow_destructive_actions=approve_destructive_action,
                preview=import_preview,
            )
            _clear_cached_import_preview()
            st.success(f"Successfully processed the Excel file for {action} action.")
        except DestructiveActionApprovalRequired as exc:
            st.error(str(exc))
        except SchemaChangeApprovalRequired as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"Unable to process the Excel file: {exc}")
    else:
        st.error("Please upload an Excel file.")

def _run_analytics_action(event_type: str, runner):
    """Execute an analytics action with audit logging and UI-safe errors."""
    try:
        df_full = read_sql_query("SELECT * FROM PRODUCT", db_path)
        result = runner(df_full)
        append_audit_event(
            db_path,
            event_type,
            {
                "status": result.status,
                "model_name": result.model_name,
                "error": result.error,
                "row_count": len(df_full),
            },
        )
        return result.text
    except Exception as exc:
        append_audit_event(
            db_path,
            event_type,
            {
                "status": "failed",
                "error": str(exc),
            },
        )
        st.error(f"Unable to complete {event_type.replace('_', ' ')}: {exc}")
        return None


# --------------------------
# Inventory Insights Section
# --------------------------
st.markdown('<h2>Generate Inventory Insights</h2>', unsafe_allow_html=True)
if st.button("Generate Insights"):
    insights = _run_analytics_action("analytics_insights", generate_insights)
    if insights is not None:
        st.write("Inventory Insights:", insights)

# --------------------------
# Stock Prediction Section
# --------------------------
st.markdown('<h2>Predict Stock Needs</h2>', unsafe_allow_html=True)
if st.button("Predict Stock Needs"):
    predictions = _run_analytics_action("analytics_stock_prediction", predict_stock_needs)
    if predictions is not None:
        st.write("Stock Predictions:", predictions)

# --------------------------
# Product Categorization Section
# --------------------------
st.markdown('<h2>Categorize Product</h2>', unsafe_allow_html=True)
product_name = st.text_input("Enter the product name:")
product_description = st.text_area("Enter the product description:")

if st.button("Categorize Product"):
    if product_name and product_description:
        category = _run_analytics_action(
            "analytics_categorize_product",
            lambda df: categorize_product(df, product_name, product_description),
        )
        if category is not None:
            st.write("Product Category:", category)
    else:
        st.error("Please provide both product name and description.")

# --------------------------
# Inventory Report Section
# --------------------------
st.markdown('<h2>Generate Inventory Report</h2>', unsafe_allow_html=True)
if st.button("Generate Report"):
    report = _run_analytics_action("analytics_report", generate_report)
    if report is not None:
        st.write("Inventory Report:", report)
