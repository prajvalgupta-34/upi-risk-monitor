import hashlib
import json
import os
import sqlite3
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score
from db import get_connection

GENESIS_HASH = "0" * 64
DATA_DIR = "data"


def compute_hash(prev_hash, txn_id, user_id, risk_score, risk_tier, rule, ts_str):
    payload = f"{prev_hash}|{txn_id}|{user_id}|{risk_score}|{risk_tier}|{rule}|{ts_str}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign_tier(score):
    if score >= 86:
        return "CRITICAL"
    if score >= 66:
        return "HIGH"
    if score >= 36:
        return "MEDIUM"
    return "LOW"


def evaluate_models(df, threshold=66):
    y_true = df["is_fraud"].astype(int).values
    candidates = {
        "SQL Rules Only": df["risk_score"].astype(float).values,
        "IsolationForest Only": df["ml_score"].astype(float).values,
        "Hybrid (50% SQL / 50% ML)": np.clip(np.round(0.50 * df["risk_score"].astype(float) + 0.50 * df["ml_score"]), 0, 100),
        "Hybrid (75% SQL / 25% ML)": df["final_score"].astype(float).values,
    }

    rows = []
    for name, scores in candidates.items():
        y_pred = (scores >= threshold).astype(int)
        rows.append({
            "model": name,
            "threshold": threshold,
            "precision": round(float(precision_score(y_true, y_pred, zero_division=0)), 4),
            "recall": round(float(recall_score(y_true, y_pred, zero_division=0)), 4),
            "f1": round(float(f1_score(y_true, y_pred, zero_division=0)), 4),
            "pr_auc": round(float(average_precision_score(y_true, scores / 100.0)), 4),
            "flagged_count": int(y_pred.sum()),
        })

    return {
        "total_txns": int(len(y_true)),
        "fraud_txns": int(y_true.sum()),
        "fraud_rate_pct": round(100.0 * float(y_true.mean()), 2),
        "default_threshold": threshold,
        "models": rows,
    }


def verify_audit_chain(cur):
    cur.execute("""
        SELECT event_id, txn_id, user_id, risk_score, risk_tier,
               triggered_rule, event_timestamp, prev_hash, current_hash
        FROM risk_events
        ORDER BY event_id ASC
    """)
    rows = cur.fetchall()
    expected_prev = GENESIS_HASH

    for row in rows:
        event_id, txn_id, user_id, score, tier, rule, ts, prev_h, curr_h = row
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        recomputed = compute_hash(prev_h, txn_id, user_id, score, tier, rule, ts_str)
        if prev_h != expected_prev or curr_h != recomputed:
            raise ValueError(f"Audit chain broken at event_id={event_id}")
        expected_prev = curr_h

    return len(rows)


def export_sqlite_snapshot(mysql_conn):
    os.makedirs(DATA_DIR, exist_ok=True)
    db_path = os.path.join(DATA_DIR, "upi_demo.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    sq_conn = sqlite3.connect(db_path)
    tables = ["admin_roles", "users", "transactions", "risk_events"]
    cur = mysql_conn.cursor()

    for table in tables:
        cur.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        df_t = pd.DataFrame(rows, columns=cols)
        for col in df_t.columns:
            if "timestamp" in col or col == "created_at":
                df_t[col] = pd.to_datetime(df_t[col]).dt.strftime("%Y-%m-%d %H:%M:%S")
            elif df_t[col].dtype == object:
                # convert MySQL Decimal values to float for SQLite numeric queries
                sample = df_t[col].dropna().iloc[0] if not df_t[col].dropna().empty else None
                if sample is not None and type(sample).__name__ == "Decimal":
                    df_t[col] = df_t[col].astype(float)
        df_t.to_sql(table, sq_conn, index=False, if_exists="replace")

    # indexes matching MySQL access patterns
    sq_conn.execute("CREATE INDEX idx_txns_user_ts ON transactions(user_id, txn_timestamp)")
    sq_conn.execute("CREATE INDEX idx_risk_txn ON risk_events(txn_id)")
    sq_conn.execute("CREATE INDEX idx_risk_user ON risk_events(user_id)")
    sq_conn.commit()
    sq_conn.close()
    cur.close()
    print(f"Exported Streamlit Cloud snapshot to {db_path}")


def main():
    conn = get_connection()
    cur = conn.cursor()

    print("Fetching scored features from v_txn_risk_scores...")
    cur.execute("""
        SELECT txn_id, user_id, amount_inr, txn_timestamp,
               txns_10m, spend_10m, secs_since_prev, is_fraud, risk_score, triggered_rule
        FROM v_txn_risk_scores
        ORDER BY txn_timestamp ASC, txn_id ASC
    """)
    cols = [d[0] for d in cur.description]
    df = pd.DataFrame(cur.fetchall(), columns=cols)

    df["amount_inr"] = df["amount_inr"].astype(float)
    df["spend_10m"] = df["spend_10m"].astype(float)
    df["secs_since_prev"] = df["secs_since_prev"].fillna(86400).astype(float)
    df["txn_hour"] = pd.to_datetime(df["txn_timestamp"]).dt.hour

    features = df[["amount_inr", "txns_10m", "spend_10m", "secs_since_prev", "txn_hour"]]

    print("Training IsolationForest anomaly scorer...")
    iso = IsolationForest(n_estimators=100, contamination=0.03, random_state=42, n_jobs=-1)
    iso.fit(features)

    raw_anomaly = -iso.decision_function(features)
    denom = raw_anomaly.max() - raw_anomaly.min()
    df["ml_score"] = (100.0 * (raw_anomaly - raw_anomaly.min()) / denom) if denom > 0 else 0.0

    df["final_score"] = np.clip(
        np.round(0.75 * df["risk_score"].astype(float) + 0.25 * df["ml_score"]),
        0, 100
    ).astype(int)
    df["final_tier"] = df["final_score"].apply(assign_tier)

    # evaluate all model configurations and persist metrics for Tab 2
    os.makedirs(DATA_DIR, exist_ok=True)
    eval_summary = evaluate_models(df, threshold=66)
    with open(os.path.join(DATA_DIR, "model_metrics.json"), "w", encoding="utf-8") as f:
        json.dump(eval_summary, f, indent=2)

    print("\nModel Evaluation (Threshold >= 66, Fraud Rate = {:.2f}%):".format(eval_summary["fraud_rate_pct"]))
    print(pd.DataFrame(eval_summary["models"]).to_string(index=False))

    flagged = df[df["final_score"] >= 36].copy()
    print(f"\nFlagged {len(flagged)} elevated risk events (score >= 36). Building hash chain...")

    records = []
    prev_hash = GENESIS_HASH

    for row in flagged.itertuples(index=False):
        rule = row.triggered_rule
        if row.ml_score >= 70 and "ML_ANOMALY" not in rule:
            rule = f"{rule}|ML_ANOMALY" if rule != "NORMAL" else "ML_ANOMALY"

        ts_str = row.txn_timestamp.strftime("%Y-%m-%d %H:%M:%S")
        curr_hash = compute_hash(
            prev_hash, int(row.txn_id), int(row.user_id),
            int(row.final_score), row.final_tier, rule, ts_str
        )
        records.append((
            int(row.txn_id), int(row.user_id), int(row.final_score),
            row.final_tier, rule, "OPEN", None, ts_str, prev_hash, curr_hash
        ))
        prev_hash = curr_hash

    cur.execute("TRUNCATE TABLE risk_events")
    insert_sql = """
        INSERT INTO risk_events (
            txn_id, user_id, risk_score, risk_tier, triggered_rule,
            action_status, reviewed_by, event_timestamp, prev_hash, current_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    cur.executemany(insert_sql, records)
    conn.commit()

    verified_count = verify_audit_chain(cur)
    print(f"Audit chain verified: {verified_count} / {verified_count} events intact.")

    export_sqlite_snapshot(conn)
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()