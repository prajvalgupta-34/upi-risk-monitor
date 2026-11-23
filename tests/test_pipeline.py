import hashlib
import json
import os
import sqlite3
import pandas as pd

SQLITE_PATH = os.path.join("data", "upi_demo.db")
METRICS_PATH = os.path.join("data", "model_metrics.json")
GENESIS_HASH = "0" * 64


def verify_chain_rows(rows):
    expected_prev = GENESIS_HASH
    for event_id, txn_id, user_id, score, tier, rule, ts, prev_h, curr_h in rows:
        ts_str = pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        payload = f"{prev_h}|{txn_id}|{user_id}|{score}|{tier}|{rule}|{ts_str}"
        recomputed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if prev_h != expected_prev or curr_h != recomputed:
            return False, int(event_id)
        expected_prev = curr_h
    return True, None


def test_transaction_reconciliation():
    assert os.path.exists(SQLITE_PATH), "data/upi_demo.db missing—run scripts/build_risk_events.py"
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM transactions")
    total = cur.fetchone()[0]
    assert total == 50000

    cur.execute("""
        SELECT
            SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END)
        FROM transactions
    """)
    success, failed, pending = cur.fetchone()
    assert success + failed + pending == total

    # verify bank-level failure rates reconcile with raw counts
    cur.execute("""
        SELECT issuer_bank, COUNT(*), SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END)
        FROM transactions
        GROUP BY issuer_bank
    """)
    bank_rows = cur.fetchall()
    assert sum(r[1] for r in bank_rows) == total
    assert sum(r[2] for r in bank_rows) == failed
    conn.close()


def test_model_metrics_file():
    assert os.path.exists(METRICS_PATH), "data/model_metrics.json missing"
    with open(METRICS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["total_txns"] == 50000
    assert data["fraud_txns"] == 1500
    hybrid = [m for m in data["models"] if "75% SQL" in m["model"]][0]
    assert 0.85 <= hybrid["pr_auc"] <= 1.0


def test_sha256_chain_and_tamper_detection():
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT event_id, txn_id, user_id, risk_score, risk_tier,
               triggered_rule, event_timestamp, prev_hash, current_hash
        FROM risk_events
        ORDER BY event_id ASC
    """)
    rows = cur.fetchall()
    conn.close()

    assert len(rows) > 0
    ok, broken_id = verify_chain_rows(rows)
    assert ok is True and broken_id is None

    # tamper with 1 risk_score in memory and confirm verification catches it
    tampered = list(rows)
    first = list(tampered[0])
    first[3] = int(first[3]) - 1  # alter risk_score by 1 point
    tampered[0] = tuple(first)

    ok_tampered, caught_id = verify_chain_rows(tampered)
    assert ok_tampered is False
    assert caught_id == int(rows[0][0])