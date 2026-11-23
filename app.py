import hashlib
import json
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from scripts.db import get_connection

st.set_page_config(page_title="UPI Risk & Payments Monitor", layout="wide")

GENESIS_HASH = "0" * 64
THRESHOLD = 66
METRICS_PATH = os.path.join("data", "model_metrics.json")


def format_inr(amount):
    val = float(amount or 0)
    if val >= 1e7:
        return f"₹{val / 1e7:.2f} Cr"
    if val >= 1e5:
        return f"₹{val / 1e5:.2f} L"
    return f"₹{val:,.0f}"


def run_query(sql, params=None):
    conn, backend = get_connection()
    if backend == "sqlite":
        sql = sql.replace("%s", "?")
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cur.close()
    conn.close()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=60)
def get_active_backend():
    conn, backend = get_connection()
    conn.close()
    return backend


@st.cache_data(ttl=60)
def load_model_metrics():
    if os.path.exists(METRICS_PATH):
        with open(METRICS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


@st.cache_data(ttl=60)
def load_roles():
    return run_query("""
        SELECT admin_id, username, role_name,
               can_approve_hold, can_freeze_account, can_adjust_thresholds
        FROM admin_roles
        ORDER BY admin_id
    """)


@st.cache_data(ttl=60)
def load_funnel_data():
    summary = run_query("""
        SELECT
            COUNT(*) AS total_txns,
            SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS success_txns,
            SUM(amount_inr) AS total_gmv,
            SUM(CASE WHEN status IN ('FAILED', 'PENDING') THEN amount_inr ELSE 0 END) AS lost_gmv,
            AVG(latency_ms) AS avg_latency
        FROM transactions
    """)

    by_bank = run_query("""
        SELECT
            issuer_bank,
            COUNT(*) AS total_txns,
            ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct,
            ROUND(SUM(CASE WHEN status IN ('FAILED', 'PENDING') THEN amount_inr ELSE 0 END), 2) AS lost_gmv_inr,
            ROUND(AVG(latency_ms), 0) AS avg_latency_ms
        FROM transactions
        GROUP BY issuer_bank
        ORDER BY failure_rate_pct DESC
    """)

    by_reason = run_query("""
        SELECT
            issuer_bank,
            failure_reason,
            COUNT(*) AS txn_count,
            ROUND(SUM(amount_inr), 2) AS impacted_gmv_inr
        FROM transactions
        WHERE status IN ('FAILED', 'PENDING')
        GROUP BY issuer_bank, failure_reason
        ORDER BY impacted_gmv_inr DESC
    """)

    raw_hours = run_query("""
        SELECT
            txn_timestamp,
            status,
            amount_inr
        FROM transactions
    """)
    raw_hours["txn_hour"] = pd.to_datetime(raw_hours["txn_timestamp"]).dt.hour
    raw_hours["amount_inr"] = pd.to_numeric(raw_hours["amount_inr"], errors="coerce")

    by_hour = (
        raw_hours.groupby("txn_hour", as_index=False)
        .agg(
            total_txns=("status", "count"),
            failed_txns=("status", lambda s: (s == "FAILED").sum()),
            lost_gmv_inr=(
                "amount_inr",
                lambda a: a[raw_hours.loc[a.index, "status"].isin(["FAILED", "PENDING"])].sum(),
            ),
        )
        .sort_values("txn_hour")
    )
    by_hour["failure_rate_pct"] = (
        100.0 * by_hour["failed_txns"] / by_hour["total_txns"]
    ).round(2)
    by_hour["lost_gmv_inr"] = by_hour["lost_gmv_inr"].round(2)

    for df in (summary, by_bank, by_reason, by_hour):
        for col in df.columns:
            if col not in ("issuer_bank", "failure_reason"):
                df[col] = pd.to_numeric(df[col], errors="coerce")

    return summary.iloc[0], by_bank, by_reason, by_hour


@st.cache_data(ttl=60)
def load_simulation_base():
    df = run_query("""
        SELECT
            t.txn_id,
            t.amount_inr,
            t.is_fraud,
            COALESCE(r.risk_score, 0) AS risk_score
        FROM transactions t
        LEFT JOIN risk_events r ON t.txn_id = r.txn_id
    """)
    df["amount_inr"] = df["amount_inr"].astype(float)
    df["is_fraud"] = df["is_fraud"].astype(int)
    df["risk_score"] = df["risk_score"].astype(int)
    return df


@st.cache_data(ttl=60)
def load_risk_feed():
    df = run_query("""
        SELECT
            u.user_id,
            u.full_name,
            u.vpa_handle,
            u.primary_bank,
            u.account_status,
            COUNT(r.event_id) AS flagged_txns,
            MAX(r.risk_score) AS peak_score,
            SUM(t.amount_inr) AS flagged_volume_inr,
            MAX(r.action_status) AS latest_action
        FROM risk_events r
        JOIN users u ON r.user_id = u.user_id
        JOIN transactions t ON r.txn_id = t.txn_id
        GROUP BY u.user_id, u.full_name, u.vpa_handle, u.primary_bank, u.account_status
        ORDER BY peak_score DESC, flagged_volume_inr DESC
    """)
    df["flagged_volume_inr"] = df["flagged_volume_inr"].astype(float)
    return df


def load_user_timeline(user_id):
    df = run_query("""
        SELECT
            t.txn_timestamp,
            t.amount_inr,
            t.merchant_category,
            t.device_id,
            COALESCE(r.risk_score, 5) AS risk_score,
            COALESCE(r.risk_tier, 'LOW') AS risk_tier,
            COALESCE(r.triggered_rule, 'NORMAL') AS triggered_rule
        FROM transactions t
        LEFT JOIN risk_events r ON t.txn_id = r.txn_id
        WHERE t.user_id = %s
        ORDER BY t.txn_timestamp ASC
    """, (int(user_id),))
    df["amount_inr"] = df["amount_inr"].astype(float)
    df["risk_score"] = df["risk_score"].astype(int)
    return df


def verify_audit_chain():
    df = run_query("""
        SELECT event_id, txn_id, user_id, risk_score, risk_tier,
               triggered_rule, event_timestamp, prev_hash, current_hash
        FROM risk_events
        ORDER BY event_id ASC
    """)
    expected_prev = GENESIS_HASH
    for row in df.itertuples(index=False):
        ts_str = pd.Timestamp(row.event_timestamp).strftime("%Y-%m-%d %H:%M:%S")
        payload = f"{row.prev_hash}|{row.txn_id}|{row.user_id}|{row.risk_score}|{row.risk_tier}|{row.triggered_rule}|{ts_str}"
        recomputed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if row.prev_hash != expected_prev or row.current_hash != recomputed:
            return False, int(row.event_id), len(df)
        expected_prev = row.current_hash
    return True, None, len(df)


def update_account_disposition(user_id, account_status, action_status, admin_id):
    conn, backend = get_connection()
    ph = "%s" if backend == "mysql" else "?"
    if backend == "mysql":
        conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE users SET account_status = {ph} WHERE user_id = {ph}",
            (account_status, int(user_id))
        )
        cur.execute(
            f"UPDATE risk_events SET action_status = {ph}, reviewed_by = {ph} WHERE user_id = {ph}",
            (action_status, int(admin_id), int(user_id))
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# --- Sidebar: RBAC Permission Simulator ---
backend_name = get_active_backend()
roles_df = load_roles()

st.sidebar.header("RBAC Simulator (Demo Mode)")
st.sidebar.caption(
    "Simulates role-based permissions for portfolio demonstration—not production authentication."
)

role_labels = [f"{r.role_name} ({r.username})" for r in roles_df.itertuples()]
selected_label = st.sidebar.selectbox("Active Role", options=role_labels)
active_role = roles_df.iloc[role_labels.index(selected_label)]

st.sidebar.markdown("**Permissions**")
st.sidebar.write(f"Approve / Hold: {'Yes' if active_role['can_approve_hold'] else 'No'}")
st.sidebar.write(f"Freeze Account: {'Yes' if active_role['can_freeze_account'] else 'No'}")
st.sidebar.write(f"Adjust Thresholds: {'Yes' if active_role['can_adjust_thresholds'] else 'No'}")

st.sidebar.divider()
if st.sidebar.button("Verify SHA-256 Audit Chain", width="stretch"):
    ok, broken_id, total_events = verify_audit_chain()
    if ok:
        st.sidebar.success(f"Chain intact ({total_events} / {total_events} events)")
    else:
        st.sidebar.error(f"Tampering detected at event_id={broken_id}")

st.sidebar.divider()
st.sidebar.caption(
    f"Data Source: **{'MySQL 8.0' if backend_name == 'mysql' else 'SQLite Demo Snapshot'}**"
)


# --- Main Header & Disclosure ---
st.title("UPI Payment Operations & Fraud Risk Monitor")
st.caption(
    "Simulated dataset: 50,000 synthetic UPI transactions across 1,000 accounts (3.00% injected fraud rate) "
    "for payment funnel diagnostics and hybrid risk scoring."
)

tab1, tab2 = st.tabs(["Payment Operations & Funnel", "Fraud Ops & Risk Rising Feed"])


# --- Tab 1: Payment Operations & Funnel ---
with tab1:
    summary, by_bank, by_reason, by_hour = load_funnel_data()
    success_rate = 100.0 * summary["success_txns"] / summary["total_txns"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total UPI Txns", f"{int(summary['total_txns']):,}")
    c2.metric("Success Rate", f"{success_rate:.2f}%")
    c3.metric("Lost GMV", format_inr(summary["lost_gmv"]))
    c4.metric("Avg Gateway Latency", f"{summary['avg_latency']:.0f} ms")

    top_fail_bank = by_bank.iloc[0]
    top_gmv_bank = by_bank.sort_values("lost_gmv_inr", ascending=False).iloc[0]
    peak_fail = by_hour[by_hour["txn_hour"].between(18, 21)]["failure_rate_pct"].mean()
    offpeak_fail = by_hour[~by_hour["txn_hour"].between(18, 21)]["failure_rate_pct"].mean()

    col_left, col_right = st.columns(2)

    with col_left:
        st.info(
            f"**Insight:** {top_fail_bank['issuer_bank']} has the highest failure rate "
            f"({top_fail_bank['failure_rate_pct']:.2f}%), while {top_gmv_bank['issuer_bank']} drives the "
            f"largest Lost GMV ({format_inr(top_gmv_bank['lost_gmv_inr'])}) due to higher transaction volume."
        )
        fig_bank = px.bar(
            by_bank,
            x="issuer_bank",
            y="failure_rate_pct",
            text="failure_rate_pct",
            color="failure_rate_pct",
            color_continuous_scale="Reds",
            hover_data={"lost_gmv_inr": ":,.0f", "total_txns": ":,", "avg_latency_ms": ":.0f"},
            title="Bank-wise Failure Rate (%)",
            labels={
                "issuer_bank": "Issuer Bank",
                "failure_rate_pct": "Failure Rate (%)",
                "lost_gmv_inr": "Lost GMV (₹)",
                "total_txns": "Total Txns",
                "avg_latency_ms": "Avg Latency (ms)",
            },
        )
        fig_bank.update_traces(texttemplate="%{text:.2f}%", textposition="outside")
        fig_bank.update_layout(coloraxis_colorbar=dict(title="Failure %"))
        st.plotly_chart(fig_bank, width="stretch")

    with col_right:
        st.info(
            f"**Insight:** Peak evening hours (18:00–21:00) average a **{peak_fail:.2f}%** failure rate "
            f"versus **{offpeak_fail:.2f}%** off-peak—primarily driven by issuer bank timeouts."
        )
        fig_hour = px.line(
            by_hour,
            x="txn_hour",
            y="failure_rate_pct",
            markers=True,
            title="Hourly Failure Rate (18:00–21:00 Peak)",
            labels={"txn_hour": "Hour of Day", "failure_rate_pct": "Failure Rate (%)"},
        )
        fig_hour.add_vrect(x0=18, x1=21, fillcolor="red", opacity=0.12, line_width=0, annotation_text="Peak Hours")
        st.plotly_chart(fig_hour, width="stretch")

    st.subheader("Lost GMV Breakdown by Bank & Failure Reason")
    top_reason = by_reason.groupby("failure_reason")["impacted_gmv_inr"].sum().idxmax()
    st.info(
        f"**Insight:** `{top_reason}` is the single largest contributor to Lost GMV across issuers, "
        f"making gateway retry logic and smart routing the highest-leverage operational fix."
    )
    fig_reason = px.bar(
        by_reason,
        x="issuer_bank",
        y="impacted_gmv_inr",
        color="failure_reason",
        barmode="stack",
        labels={"issuer_bank": "Issuer Bank", "impacted_gmv_inr": "Lost GMV (₹)", "failure_reason": "Failure Reason"},
    )
    st.plotly_chart(fig_reason, width="stretch")


# --- Tab 2: Fraud Ops & Risk Rising Feed ---
with tab2:
    metrics_data = load_model_metrics()
    if metrics_data:
        st.subheader("Fraud Model Evaluation (Simulated Labels — 3.00% Class Imbalance)")
        validation_label = metrics_data.get("validation_method", "5-fold stratified out-of-fold")
        st.caption(
            f"Evaluated via {validation_label} across 50,000 simulated transactions (3.00% fraud rate) "
            f"at default block threshold (>= {THRESHOLD}). PR-AUC is prioritized over ROC-AUC under class imbalance."
        )
        eval_df = pd.DataFrame(metrics_data["models"])
        eval_df.columns = ["Model Variant", "Threshold", "Precision", "Recall", "F1 Score", "PR-AUC", "Flagged Txns"]
        st.dataframe(eval_df, width="stretch", hide_index=True)
        st.divider()

    st.subheader("What-If Fraud Threshold Simulator")
    can_adjust = bool(active_role["can_adjust_thresholds"])
    threshold = st.slider(
        "Auto-Block Risk Score Threshold",
        min_value=36,
        max_value=95,
        value=THRESHOLD,
        step=1,
        disabled=not can_adjust,
        help="Switch to RISK_ADMIN in the sidebar to modify live block thresholds.",
    )
    if not can_adjust:
        st.caption("Threshold adjustment is locked for your role. Switch to RISK_ADMIN in the sidebar to simulate changes.")

    sim_df = load_simulation_base()
    flagged_mask = sim_df["risk_score"] >= threshold
    fraud_mask = sim_df["is_fraud"] == 1
    legit_mask = sim_df["is_fraud"] == 0

    fraud_prevented = sim_df.loc[flagged_mask & fraud_mask, "amount_inr"].sum()
    total_fraud_val = sim_df.loc[fraud_mask, "amount_inr"].sum()
    capture_pct = 100.0 * fraud_prevented / total_fraud_val if total_fraud_val > 0 else 0.0

    false_positives = int((flagged_mask & legit_mask).sum())
    fpr_pct = 100.0 * false_positives / int(legit_mask.sum())
    blocked_legit_gmv = sim_df.loc[flagged_mask & legit_mask, "amount_inr"].sum()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Simulated Fraud Prevented", format_inr(fraud_prevented))
    m2.metric("Fraud Value Capture", f"{capture_pct:.2f}%")
    m3.metric("False Positive Rate", f"{fpr_pct:.3f}%")
    m4.metric("Blocked Legit GMV", format_inr(blocked_legit_gmv))

    st.divider()
    st.subheader("Risk Rising Account Feed")
    feed_df = load_risk_feed()
    st.dataframe(feed_df.head(25), width="stretch", hide_index=True)

    st.subheader("Inspect Account Timeline & Take Action")
    user_options = [
        f"User {r.user_id} | {r.vpa_handle} (Peak: {r.peak_score}, Status: {r.account_status})"
        for r in feed_df.head(30).itertuples()
    ]
    chosen = st.selectbox("Select Flagged Account", options=user_options)
    selected_uid = int(chosen.split("|")[0].replace("User", "").strip())

    timeline_df = load_user_timeline(selected_uid)
    timeline_df["txn_timestamp"] = pd.to_datetime(timeline_df["txn_timestamp"])

    zoom_burst = st.toggle("Zoom to Peak Risk Burst Window", value=True)
    plot_df = timeline_df
    if zoom_burst and not timeline_df.empty:
        peak_ts = timeline_df.loc[timeline_df["risk_score"].idxmax(), "txn_timestamp"]
        window_mask = (
            (timeline_df["txn_timestamp"] >= peak_ts - pd.Timedelta(minutes=25)) &
            (timeline_df["txn_timestamp"] <= peak_ts + pd.Timedelta(minutes=10))
        )
        plot_df = timeline_df.loc[window_mask]

    fig_tl = go.Figure()
    fig_tl.add_trace(go.Scatter(
        x=plot_df["txn_timestamp"],
        y=plot_df["risk_score"],
        mode="lines+markers",
        name="Risk Score",
        customdata=plot_df[["amount_inr", "merchant_category", "triggered_rule"]],
        hovertemplate="Time: %{x}<br>Score: %{y}<br>Amount: ₹%{customdata[0]:,.2f}<br>Category: %{customdata[1]}<br>Rule: %{customdata[2]}<extra></extra>",
    ))
    fig_tl.add_hline(y=36, line_dash="dot", line_color="gold", annotation_text="MEDIUM (OTP)")
    fig_tl.add_hline(y=66, line_dash="dash", line_color="orange", annotation_text="HIGH (HOLD)")
    fig_tl.add_hline(y=86, line_dash="solid", line_color="red", annotation_text="CRITICAL (FREEZE)")
    fig_tl.update_layout(
        title=f"Escalating Risk Timeline — User {selected_uid}",
        xaxis_title="Transaction Timestamp",
        yaxis_title="Risk Score (0-100)",
        yaxis=dict(range=[0, 105]),
    )
    st.plotly_chart(fig_tl, width="stretch")

    b1, b2, b3 = st.columns(3)
    can_hold = bool(active_role["can_approve_hold"])
    can_freeze = bool(active_role["can_freeze_account"])

    if b1.button("Approve Account", disabled=not can_hold, width="stretch"):
        update_account_disposition(selected_uid, "ACTIVE", "APPROVED", active_role["admin_id"])
        st.cache_data.clear()
        st.success(f"User {selected_uid} marked ACTIVE / APPROVED.")
        st.rerun()

    if b2.button("Place on Hold", disabled=not can_hold, width="stretch"):
        update_account_disposition(selected_uid, "HOLD", "HELD", active_role["admin_id"])
        st.cache_data.clear()
        st.warning(f"User {selected_uid} placed on HOLD.")
        st.rerun()

    if b3.button("Freeze Account", disabled=not can_freeze, type="primary", width="stretch"):
        update_account_disposition(selected_uid, "FROZEN", "FROZEN", active_role["admin_id"])
        st.cache_data.clear()
        st.error(f"User {selected_uid} FROZEN via atomic transaction.")
        st.rerun()