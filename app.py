
import os
import re
import time
from datetime import datetime
from typing import Dict

import pandas as pd
import plotly.express as px
import streamlit as st

# ===========================
# Secrets loading strategy
# Priority (first non-empty wins):
# 1) OS environment
# 2) .env (python-dotenv)
# 3) Streamlit Cloud secrets (st.secrets)
# 4) Local secrets.toml (for local dev)
# ===========================
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

toml_config: Dict = {}
# Try Streamlit Cloud secrets
try:
    if hasattr(st, "secrets") and st.secrets:
        toml_config = dict(st.secrets)
except Exception:
    toml_config = {}

# Fallback: local secrets.toml (dev)
if not toml_config:
    try:
        import tomllib  # Python 3.11+
    except Exception:
        try:
            import tomli as tomllib  # older Python
        except Exception:
            tomllib = None
    if tomllib and os.path.exists("secrets.toml"):
        try:
            with open("secrets.toml", "rb") as f:
                toml_config = tomllib.load(f)
        except Exception:
            toml_config = {}

def _from_toml(key: str):
    if not toml_config:
        return None
    k = key.lower()
    # Check common sections
    for section in ("snowflake", "openai", "audit"):
        sec = toml_config.get(section)
        if isinstance(sec, dict):
            if k in sec:
                return str(sec[k])
            if key in sec:
                return str(sec[key])
    # Allow flat keys too (Streamlit supports both styles)
    if key in toml_config:
        return str(toml_config[key])
    if k in toml_config:
        return str(toml_config[k])
    return None

def get_secret(key: str, default: str = "") -> str:
    """OS env → .env → st.secrets / secrets.toml → default"""
    return os.getenv(key) or _from_toml(key) or default

# Optional: OpenAI (or compatible) LLM for NL→SQL.
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# Snowflake connector
import snowflake.connector

# ---------------------------
# Page config
# ---------------------------
st.set_page_config(page_title="Chat with my Data!", layout="wide")
st.title("Chat with my data!")
st.caption("Ask a question; I’ll generate safe SQL for Snowflake, run it, pick suitable visuals, and suggest insights.")

# ---------------------------
# Sidebar: connections & settings
# ---------------------------
with st.sidebar:
    st.header("🔐 Connections")
    use_env = st.toggle("Use environment variables (.env / Secrets / OS env)", value=True)

    if use_env:
        # Read with fallbacks (support both sectioned and flat keys)
        SNOWFLAKE_ACCOUNT   = get_secret("SNOWFLAKE_ACCOUNT", get_secret("account", ""))
        SNOWFLAKE_USER      = get_secret("SNOWFLAKE_USER", get_secret("user", ""))
        SNOWFLAKE_PASSWORD  = get_secret("SNOWFLAKE_PASSWORD", get_secret("password", ""))
        SNOWFLAKE_WAREHOUSE = get_secret("SNOWFLAKE_WAREHOUSE", get_secret("warehouse", "ETL_RUN_WH"))
        SNOWFLAKE_DATABASE  = get_secret("SNOWFLAKE_DATABASE", get_secret("database", "SNOWFLAKE_SAMPLE_DATA"))
        SNOWFLAKE_SCHEMA    = get_secret("SNOWFLAKE_SCHEMA", get_secret("schema", "TPCH_SF100"))
        OPENAI_API_KEY      = get_secret("OPENAI_API_KEY", get_secret("api_key", ""))
        OPENAI_MODEL        = get_secret("OPENAI_MODEL", get_secret("model", "gpt-4o-mini"))
        AUDIT_DB            = get_secret("AUDIT_DB", get_secret("db", SNOWFLAKE_DATABASE))
        AUDIT_SCHEMA        = get_secret("AUDIT_SCHEMA", get_secret("audit_schema", get_secret("schema", "AUDIT_SCHEMA")))
        AUDIT_TABLE         = get_secret("AUDIT_TABLE", get_secret("table", "CHAT_DATA_AUDIT"))

        # Ensure downstream code using os.getenv(...) can see values
        for k, v in {
            "SNOWFLAKE_ACCOUNT": SNOWFLAKE_ACCOUNT,
            "SNOWFLAKE_USER": SNOWFLAKE_USER,
            "SNOWFLAKE_PASSWORD": SNOWFLAKE_PASSWORD,
            "SNOWFLAKE_WAREHOUSE": SNOWFLAKE_WAREHOUSE,
            "SNOWFLAKE_DATABASE": SNOWFLAKE_DATABASE,
            "SNOWFLAKE_SCHEMA": SNOWFLAKE_SCHEMA,
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "OPENAI_MODEL": OPENAI_MODEL,
            "AUDIT_DB": AUDIT_DB,
            "AUDIT_SCHEMA": AUDIT_SCHEMA,
            "AUDIT_TABLE": AUDIT_TABLE,
        }.items():
            if v and not os.getenv(k):
                os.environ[k] = v
    else:
        SNOWFLAKE_ACCOUNT = st.text_input("SNOWFLAKE_ACCOUNT")
        SNOWFLAKE_USER = st.text_input("SNOWFLAKE_USER")
        SNOWFLAKE_PASSWORD = st.text_input("SNOWFLAKE_PASSWORD", type="password")
        SNOWFLAKE_WAREHOUSE = st.text_input("SNOWFLAKE_WAREHOUSE")
        SNOWFLAKE_DATABASE = st.text_input("SNOWFLAKE_DATABASE", value="SNOWFLAKE_SAMPLE_DATA")
        SNOWFLAKE_SCHEMA = st.text_input("SNOWFLAKE_SCHEMA", value="TPCH_SF1000")
        OPENAI_API_KEY = st.text_input("OPENAI_API_KEY (optional)", type="password")
        OPENAI_MODEL = st.text_input("OPENAI_MODEL", value="gpt-4o-mini")
        AUDIT_DB = st.text_input("AUDIT_DB", value=SNOWFLAKE_DATABASE)
        AUDIT_SCHEMA = st.text_input("AUDIT_SCHEMA", value="AUDIT_SCHEMA")
        AUDIT_TABLE = st.text_input("AUDIT_TABLE", value="CHAT_DATA_AUDIT")

    st.divider()
    st.header("⚙️ Settings")
    max_rows = st.number_input("Max rows to fetch", min_value=100, max_value=50000, value=5000, step=100)
    hard_limit = st.number_input("Hard LIMIT injected into SQL (to protect UI)", min_value=100, max_value=100000, value=5000, step=100)
    timeout_s = st.number_input("Statement timeout (seconds)", min_value=5, max_value=600, value=60, step=5)
    enable_audit = st.toggle("Write audit logs (PROMPT/SQL/ROWCOUNT)", value=False, help="Writes to AUDIT_DB.AUDIT_SCHEMA.AUDIT_TABLE")
    audit_db = st.text_input("AUDIT_DB", value=os.getenv("AUDIT_DB", SNOWFLAKE_DATABASE))
    audit_schema = st.text_input("AUDIT_SCHEMA", value=os.getenv("AUDIT_SCHEMA", "AUDIT_SCHEMA"))
    audit_table = st.text_input("AUDIT_TABLE", value=os.getenv("AUDIT_TABLE", "CHAT_DATA_AUDIT"))
    audit_debug = st.toggle("Show audit errors", value=True)

# ---------------------------
# TPCH schema primer
# ---------------------------
TPCH_TABLES = {
    "CUSTOMER": ["C_CUSTKEY","C_NAME","C_ADDRESS","C_NATIONKEY","C_PHONE","C_ACCTBAL","C_MKTSEGMENT","C_COMMENT"],
    "ORDERS": ["O_ORDERKEY","O_CUSTKEY","O_ORDERSTATUS","O_TOTALPRICE","O_ORDERDATE","O_ORDERPRIORITY","O_CLERK","O_SHIPPRIORITY","O_COMMENT"],
    "LINEITEM": [
        "L_ORDERKEY","L_PARTKEY","L_SUPPKEY","L_LINENUMBER","L_QUANTITY","L_EXTENDEDPRICE","L_DISCOUNT","L_TAX",
        "L_RETURNFLAG","L_LINESTATUS","L_SHIPDATE","L_COMMITDATE","L_RECEIPTDATE","L_SHIPINSTRUCT","L_SHIPMODE","L_COMMENT"
    ],
    "NATION": ["N_NATIONKEY","N_NAME","N_REGIONKEY","N_COMMENT"],
    "REGION": ["R_REGIONKEY","R_NAME","R_COMMENT"],
    "PART": ["P_PARTKEY","P_NAME","P_MFGR","P_BRAND","P_TYPE","P_SIZE","P_CONTAINER","P_RETAILPRICE","P_COMMENT"],
    "PARTSUPP": ["PS_PARTKEY","PS_SUPPKEY","PS_AVAILQTY","PS_SUPPLYCOST","PS_COMMENT"],
    "SUPPLIER": ["S_SUPPKEY","S_NAME","S_ADDRESS","S_NATIONKEY","S_PHONE","S_ACCTBAL","S_COMMENT"],
}

def schema_block(database: str, schema: str) -> str:
    lines = [f"You can ONLY query from {database}.{schema} and only these tables/columns:"]
    for t, cols in TPCH_TABLES.items():
        lines.append(f"- {database}.{schema}.{t} (" + ", ".join(cols) + ")")
    return "\n".join(lines)

# ---------------------------
# NL → SQL
# ---------------------------
SQL_SYSTEM_PROMPT = """
You are a senior Snowflake SQL generator. Convert the user's question into a **single** safe SQL SELECT statement for Snowflake.
STRICT RULES:
- Only read from the whitelisted schema and tables the user provides.
- Output **ONLY** the SQL, no backticks, no commentary.
- Use fully qualified names <DATABASE>.<SCHEMA>.<TABLE>.
- Prefer aggregated results (GROUP BY) with explicit column aliases and don't use any KEY/ID columns for grouping.
- Push filters into WHERE; use DATE or YEAR extraction as appropriate.
- Never use DDL/DML (CREATE/UPDATE/DELETE/INSERT/MERGE/COPY) or CALL.
- Always end with a LIMIT if not provided, using the limit hint supplied.
- If the question is ambiguous, choose a reasonable default and continue.
""".strip()

def call_llm_for_sql(user_question: str, database: str, schema: str, limit_hint: int) -> str:
    whitelist = schema_block(database, schema)
    user_prompt = f"""
{whitelist}
Generate a single Snowflake SQL (no comments) answering:
"{user_question.strip()}"
Ensure the query ends with "LIMIT {limit_hint}" if not already.
""".strip()

    if OPENAI_AVAILABLE and (os.getenv("OPENAI_API_KEY") or 'OPENAI_API_KEY' in globals()):
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY") or globals().get("OPENAI_API_KEY", "")
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL") or globals().get("OPENAI_MODEL", "gpt-4o-mini")
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SQL_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
        sql = completion.choices[0].message.content.strip()
    else:
        uq = user_question.lower()
        if "revenue" in uq or "sales" in uq:
            sql = f"""
SELECT c.C_MKTSEGMENT AS SEGMENT,
       SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM {database}.{schema}.CUSTOMER c
JOIN {database}.{schema}.ORDERS o ON o.O_CUSTKEY = c.C_CUSTKEY
JOIN {database}.{schema}.LINEITEM l ON l.L_ORDERKEY = o.O_ORDERKEY
GROUP BY 1
ORDER BY 2 DESC
LIMIT {limit_hint}
""".strip()
        elif "top" in uq and "customers" in uq:
            sql = f"""
SELECT c.C_NAME AS CUSTOMER,
       SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM {database}.{schema}.CUSTOMER c
JOIN {database}.{schema}.ORDERS o ON o.O_CUSTKEY = c.C_CUSTKEY
JOIN {database}.{schema}.LINEITEM l ON l.L_ORDERKEY = o.O_ORDERKEY
GROUP BY 1
ORDER BY 2 DESC
LIMIT {limit_hint}
""".strip()
        elif "monthly" in uq and ("revenue" in uq or "sales" in uq):
            sql = f"""
SELECT DATE_TRUNC('month', o.O_ORDERDATE) AS MONTH,
       SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM {database}.{schema}.ORDERS o
JOIN {database}.{schema}.LINEITEM l ON l.L_ORDERKEY = o.O_ORDERKEY
GROUP BY 1
ORDER BY 1
LIMIT {limit_hint}
""".strip()
        else:
            sql = f"SELECT * FROM {database}.{schema}.CUSTOMER LIMIT {limit_hint}"

    sql = sql.split(";")[0].strip()
    if not re.match(r"^select\s", sql, re.IGNORECASE):
        raise ValueError("Generated SQL is not a SELECT.")

    if re.search(r"\blimit\b\s+\d+\s*$", sql, re.IGNORECASE) is None:
        sql += f"\nLIMIT {limit_hint}"

    # Ensure fully-qualified names
    lowered = sql.lower()
    if f" {database.lower()}.{schema.lower()}." not in lowered:
        for t in TPCH_TABLES:
            sql = re.sub(rf"(?i)\b{t}\b", f"{database}.{schema}.{t}", sql)

    return sql

# ---------------------------
# Snowflake helpers
# ---------------------------
def sf_connect():
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE", "SNOWFLAKE_SAMPLE_DATA"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "TPCH_SF1000"),
        client_session_keep_alive=True,
        application="Chat with my Data!",
    )

def run_query_df(sql: str, timeout_seconds: int, max_rows: int) -> pd.DataFrame:
    ctx = sf_connect()
    try:
        cs = ctx.cursor()
        try:
            cs.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS={int(timeout_seconds)}")
            cs.execute(sql)
            cols = [c[0] for c in cs.description]
            rows = cs.fetchmany(max_rows)
            return pd.DataFrame(rows, columns=cols)
        finally:
            cs.close()
    finally:
        ctx.close()


def try_audit_log(enabled: bool, prompt: str, sql: str, rowcount: int,
                  audit_db: str, audit_schema: str, audit_table: str, show_errors: bool):
    if not enabled:
        return
    qname = f"{audit_db}.{audit_schema}.{audit_table}"
    try:
        ctx = sf_connect()
        cur = ctx.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {qname} ("
                "TS TIMESTAMP_NTZ, PROMPT STRING, SQL_TEXT STRING, ROWCOUNT INTEGER)"
            )
            # ✅ Snowflake param binding (pyformat)
            cur.execute(
                f"INSERT INTO {qname} (TS, PROMPT, SQL_TEXT, ROWCOUNT) "
                "VALUES (%(ts)s, %(prompt)s, %(sql)s, %(rc)s)",
                {"ts": datetime.utcnow(), "prompt": prompt, "sql": sql, "rc": int(rowcount)},
            )
            try:
                ctx.commit()
            except Exception:
                pass
        finally:
            cur.close(); ctx.close()
    except Exception as e:
        if show_errors:
            st.warning(f"Audit log failed for {qname}: {e}")


# ---------------------------
# Smart viz & insights
# ---------------------------
NUMERIC_HINTS = ("revenue","amount","price","total","sum","avg","average","count","qty","quantity","score","rate","value","metric")

def to_datetime_if_possible(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    try:
        return pd.to_datetime(series, errors="raise")
    except Exception:
        return series

def classify_columns(df: pd.DataFrame):
    numerics, dates, cats = [], [], []
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_numeric_dtype(s):
            numerics.append(col)
        else:
            s2 = to_datetime_if_possible(s)
            if pd.api.types.is_datetime64_any_dtype(s2):
                df[col] = s2
                dates.append(col)
            else:
                cats.append(col)
    return numerics, dates, cats

def pick_metric(numerics):
    if not numerics:
        return ""
    for hint in NUMERIC_HINTS:
        for c in numerics:
            if hint in c.lower():
                return c
    return numerics[0]

def pick_dimension(cats, df, max_card=50):
    if not cats:
        return ""
    candidates = sorted(cats, key=lambda c: df[c].nunique())
    for c in candidates:
        if 2 <= df[c].nunique() <= max_card:
            return c
    return candidates[0]

def pareto_dataframe(df, dim, metric, top_n=20):
    tmp = df.groupby(dim, dropna=False)[metric].sum().reset_index()
    tmp = tmp.sort_values(metric, ascending=False)
    tmp["cum_pct"] = tmp[metric].cumsum() / tmp[metric].sum() * 100.0
    return tmp.head(top_n)

def correlation_pairs(df, numerics):
    if len(numerics) < 2:
        return ("","",0.0)
    corr = df[numerics].corr(numeric_only=True)
    best = (None, None, 0.0)
    for i, a in enumerate(numerics):
        for b in numerics[i+1:]:
            v = abs(corr.loc[a,b])
            if pd.notna(v) and v > best[2]:
                best = (a,b,float(corr.loc[a,b]))
    if best[0] is None: return ("","",0.0)
    return best

def generate_insights(df: pd.DataFrame):
    insights = []
    if df.empty:
        return ["No rows returned. Try broadening filters or lowering the LIMIT."]

    numerics, dates, cats = classify_columns(df.copy())

    if dates and numerics:
        x = dates[0]; y = pick_metric(numerics)
        d = df[[x,y]].dropna().sort_values(x)
        if len(d) >= 2:
            first, last = d[y].iloc[0], d[y].iloc[-1]
            change = (last - first)
            pct = (change / first * 100.0) if first not in (0,None) else None
            if pct is not None and pd.notna(pct):
                direction = "increased" if change > 0 else "decreased" if change < 0 else "stayed flat"
                insights.append(f"Time trend: **{y}** has {direction} by **{abs(change):,.2f}** ({abs(pct):.1f}%).")
            else:
                insights.append(f"Time trend: **{y}** changed by **{change:,.2f}** over the period.")

    if cats and numerics:
        dim = pick_dimension(cats, df)
        metric = pick_metric(numerics)
        p = pareto_dataframe(df, dim, metric, top_n=10)
        if not p.empty:
            top = p.iloc[0]
            insights.append(f"Top **{dim}** by **{metric}** is **{top[dim]}** at **{top[metric]:,.2f}**.")
            eighty = p[p["cum_pct"] >= 80.0]
            if not eighty.empty:
                k = eighty.index[0] + 1
                insights.append(f"Top **{k} {dim}** contribute ~**80%** of total **{metric}** (Pareto).")

    if len(numerics) >= 2:
        a,b,r = correlation_pairs(df, numerics)
        if a and b:
            if abs(r) >= 0.6:
                relation = "positively" if r > 0 else "negatively"
                insights.append(f"Strong correlation: **{a}** and **{b}** are {relation} correlated (r≈{r:.2f}).")
            else:
                insights.append(f"Weak correlation across numeric fields (strongest |r|≈{abs(r):.2f} between **{a}** and **{b}**).")

    if numerics:
        m = pick_metric(numerics)
        s = df[m].dropna().astype(float)
        if len(s) >= 5:
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            iqr = q3 - q1
            upper = q3 + 1.5*iqr
            n_out = int((s > upper).sum())
            if n_out > 0:
                insights.append(f"Outliers: **{n_out}** values in **{m}** exceed the upper IQR fence (~{upper:,.2f}).")

    if not insights:
        insights.append("No strong signals detected; consider refining the question or grouping/aggregating the results.")
    return insights

def render_smart_visuals(df: pd.DataFrame):
    if df.empty:
        st.info("No data to visualize.")
        return

    numerics, dates, cats = classify_columns(df.copy())
    tabs = []

    if dates and numerics:
        x = dates[0]; y = pick_metric(numerics)
        tabs.append(("Trend", px.line(df.sort_values(x), x=x, y=y)))
    if cats and numerics:
        dim = pick_dimension(cats, df)
        metric = pick_metric(numerics)
        dff = df.groupby(dim, dropna=False)[metric].sum().reset_index()
        dff = dff.sort_values(metric, ascending=False).head(50)
        tabs.append(("Ranking", px.bar(dff, x=dim, y=metric)))
        p = pareto_dataframe(df, dim, metric, top_n=20)
        tabs.append(("Pareto", px.line(p, x=dim, y="cum_pct")))
    if len(numerics) >= 2:
        a,b,_ = correlation_pairs(df, numerics)
        if a and b:
            tabs.append(("Scatter", px.scatter(df, x=a, y=b, trendline=None)))
            corr = df[numerics].corr(numeric_only=True)
            tabs.append(("Correlation", px.imshow(corr, text_auto=True)))

    if not tabs and numerics:
        m = pick_metric(numerics)
        tabs.append(("Distribution", px.histogram(df, x=m)))

    if not tabs:
        st.dataframe(df, use_container_width=True)
        return

    st.write("### Visuals")
    labels = [t[0] for t in tabs]
    figures = [t[1] for t in tabs]
    st_tabs = st.tabs(labels)
    for tab, fig in zip(st_tabs, figures):
        with tab:
            st.plotly_chart(fig, use_container_width=True)

# ---------------------------
# UI - ask + run
# ---------------------------
example_queries = [
    "Top 10 customers by revenue",
    "Monthly revenue trend for 1995",
    "Revenue by nation",
    "Average discount by ship mode",
    "Top 15 parts by total sales",
]

st.subheader("Ask a question")
col1, col2 = st.columns([4,1])
with col1:
    question = st.text_input("Your question", placeholder="e.g., Revenue by nation in 1995")
with col2:
    st.markdown("**Quick picks**")
    for q in example_queries:
        if st.button(q, use_container_width=True):
            question = q

advanced = st.expander("Advanced: generated SQL & raw data")

if st.button("🚀 Run", type="primary"):
    required = [
        os.getenv("SNOWFLAKE_ACCOUNT"),
        os.getenv("SNOWFLAKE_USER"),
        os.getenv("SNOWFLAKE_PASSWORD"),
        os.getenv("SNOWFLAKE_WAREHOUSE"),
        os.getenv("SNOWFLAKE_DATABASE"),
        os.getenv("SNOWFLAKE_SCHEMA"),
    ]
    if not all(required):
        st.error("Please provide Snowflake connection details in the sidebar or via secrets (.env / Streamlit Secrets / secrets.toml).")
        st.stop()

    if not (question and question.strip()):
        st.warning("Please enter a question.")
        st.stop()

    with st.spinner("Generating SQL from your question…"):
        try:
            database = os.getenv("SNOWFLAKE_DATABASE", "SNOWFLAKE_SAMPLE_DATA")
            schema = os.getenv("SNOWFLAKE_SCHEMA", "TPCH_SF1000")
            sql = call_llm_for_sql(question, database, schema, int(hard_limit))
        except Exception as e:
            st.error(f"Failed to generate SQL: {e}")
            st.stop()

    banned = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|COPY|CALL|GRANT|REVOKE)\b", re.IGNORECASE)
    if banned.search(sql):
        st.error("Query blocked by guardrails (non-SELECT detected).")
        st.stop()

    st.success("SQL generated")
    if enable_audit and os.getenv("SNOWFLAKE_DATABASE", "").upper() == "SNOWFLAKE_SAMPLE_DATA" and (audit_db.upper() == "SNOWFLAKE_SAMPLE_DATA"):
        st.error("Audit is enabled but AUDIT_DB is SNOWFLAKE_SAMPLE_DATA (read-only). Choose a writable database (e.g., DEMO_DB).")

    with advanced:
        st.code(sql, language="sql")

    with st.spinner("Running on Snowflake…"):
        t0 = time.time()
        try:
            df = run_query_df(sql, timeout_s, max_rows)
        except Exception as e:
            st.error(f"Query failed: {e}")
            st.stop()
        dt = time.time() - t0

    st.markdown(f"**Returned {len(df):,} rows in {dt:0.2f}s**")

    # Write audit (optional)
    try_audit_log(enable_audit, question, sql, len(df), audit_db, audit_schema, audit_table, audit_debug)

    render_smart_visuals(df)

    st.write("### Insights")
    for bullet in generate_insights(df):
        st.write(f"- {bullet}")

    with advanced:
        st.subheader("Raw results")
        st.dataframe(df, use_container_width=True)
