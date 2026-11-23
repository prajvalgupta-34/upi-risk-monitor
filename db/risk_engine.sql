USE upi_risk_monitor;

-- reusable view so python (ml + audit log) and streamlit pull from one source of truth
CREATE OR REPLACE VIEW v_txn_risk_scores AS
WITH base_features AS (
    SELECT
        t.txn_id,
        t.user_id,
        u.vpa_handle,
        u.avg_monthly_spend,
        t.issuer_bank,
        t.merchant_category,
        t.amount_inr,
        t.txn_timestamp,
        t.device_id,
        t.is_fraud,
        COUNT(*) OVER (
            PARTITION BY t.user_id
            ORDER BY t.txn_timestamp
            RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW
        ) AS txns_10m,
        SUM(t.amount_inr) OVER (
            PARTITION BY t.user_id
            ORDER BY t.txn_timestamp
            RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW
        ) AS spend_10m,
        TIMESTAMPDIFF(
            SECOND,
            LAG(t.txn_timestamp) OVER (PARTITION BY t.user_id ORDER BY t.txn_timestamp),
            t.txn_timestamp
        ) AS secs_since_prev,
        FIRST_VALUE(t.device_id) OVER (
            PARTITION BY t.user_id
            ORDER BY t.txn_timestamp
        ) AS baseline_device
    FROM transactions t
    JOIN users u ON t.user_id = u.user_id
),
scored AS (
    SELECT
        *,
        LEAST(100,
            (CASE
                WHEN txns_10m >= 6 THEN 40
                WHEN txns_10m >= 4 THEN 28
                WHEN txns_10m >= 2 THEN 15
                ELSE 0
            END) +
            (CASE
                WHEN secs_since_prev IS NOT NULL AND secs_since_prev <= 60 THEN 20
                WHEN secs_since_prev IS NOT NULL AND secs_since_prev <= 180 THEN 10
                ELSE 0
            END) +
            (CASE
                WHEN spend_10m >= avg_monthly_spend THEN 25
                WHEN amount_inr >= 0.35 * avg_monthly_spend THEN 15
                ELSE 0
            END) +
            (CASE WHEN device_id <> baseline_device THEN 5 ELSE 0 END) +
            (CASE WHEN HOUR(txn_timestamp) BETWEEN 0 AND 4 THEN 5 ELSE 0 END) +
            (CASE WHEN merchant_category IN ('CRYPTO_P2P', 'GAMING') THEN 5 ELSE 0 END)
        ) AS risk_score
    FROM base_features
)
SELECT
    txn_id,
    user_id,
    vpa_handle,
    issuer_bank,
    merchant_category,
    amount_inr,
    txn_timestamp,
    txns_10m,
    ROUND(spend_10m, 2) AS spend_10m,
    secs_since_prev,
    is_fraud,
    risk_score,
    CASE
        WHEN risk_score >= 86 THEN 'CRITICAL'
        WHEN risk_score >= 66 THEN 'HIGH'
        WHEN risk_score >= 36 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS risk_tier,
    CASE
        WHEN spend_10m >= avg_monthly_spend AND txns_10m >= 4 THEN 'ACCOUNT_DRAIN_VELOCITY'
        WHEN txns_10m >= 4 THEN 'VELOCITY_SPIKE'
        WHEN amount_inr >= 0.35 * avg_monthly_spend THEN 'AMOUNT_ANOMALY'
        WHEN secs_since_prev IS NOT NULL AND secs_since_prev <= 60 THEN 'RAPID_REPEAT'
        ELSE 'NORMAL'
    END AS triggered_rule
FROM scored;

-- 1. tier distribution check (0-35 LOW, 36-65 MEDIUM/OTP, 66-85 HIGH/HOLD, 86-100 CRITICAL/FREEZE)
SELECT
    risk_tier,
    COUNT(*) AS txn_count,
    SUM(is_fraud) AS actual_fraud_txns,
    ROUND(100.0 * SUM(is_fraud) / COUNT(*), 2) AS precision_pct,
    ROUND(SUM(amount_inr), 2) AS total_volume_inr
FROM v_txn_risk_scores
GROUP BY risk_tier
ORDER BY FIELD(risk_tier, 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL');

-- 2. business impact at default block threshold (risk_score >= 66)
SELECT
    ROUND(SUM(CASE WHEN risk_score >= 66 AND is_fraud = 1 THEN amount_inr ELSE 0 END), 2) AS fraud_loss_prevented_inr,
    ROUND(SUM(CASE WHEN risk_score < 66  AND is_fraud = 1 THEN amount_inr ELSE 0 END), 2) AS missed_fraud_inr,
    ROUND(
        100.0 * SUM(CASE WHEN risk_score >= 66 AND is_fraud = 1 THEN amount_inr ELSE 0 END) /
        NULLIF(SUM(CASE WHEN is_fraud = 1 THEN amount_inr ELSE 0 END), 0),
    2) AS fraud_value_capture_pct,
    ROUND(
        100.0 * SUM(CASE WHEN risk_score >= 66 AND is_fraud = 0 THEN 1 ELSE 0 END) /
        NULLIF(SUM(CASE WHEN is_fraud = 0 THEN 1 ELSE 0 END), 0),
    3) AS false_positive_rate_pct,
    ROUND(SUM(CASE WHEN risk_score >= 66 AND is_fraud = 0 THEN amount_inr ELSE 0 END), 2) AS blocked_legit_gmv_inr
FROM v_txn_risk_scores;

-- 3. top 10 escalating accounts for the risk rising feed
SELECT
    user_id,
    vpa_handle,
    COUNT(*) AS total_txns,
    MIN(risk_score) AS min_score,
    MAX(risk_score) AS peak_score,
    SUM(CASE WHEN risk_tier IN ('HIGH', 'CRITICAL') THEN 1 ELSE 0 END) AS high_risk_txns,
    ROUND(SUM(CASE WHEN risk_tier IN ('HIGH', 'CRITICAL') THEN amount_inr ELSE 0 END), 2) AS exposed_inr
FROM v_txn_risk_scores
GROUP BY user_id, vpa_handle
HAVING MAX(risk_score) >= 66
ORDER BY peak_score DESC, exposed_inr DESC
LIMIT 10;