USE upi_risk_monitor;

-- bank-wise failure rate and lost gmv (failed + pending rupee volume)
SELECT
    issuer_bank,
    COUNT(*) AS total_txns,
    SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS success_txns,
    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_txns,
    ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct,
    ROUND(SUM(CASE WHEN status IN ('FAILED', 'PENDING') THEN amount_inr ELSE 0 END), 2) AS lost_gmv_inr,
    ROUND(AVG(latency_ms), 0) AS avg_latency_ms
FROM transactions
GROUP BY issuer_bank
ORDER BY lost_gmv_inr DESC;

-- failure reason breakdown within each bank
SELECT
    issuer_bank,
    failure_reason,
    COUNT(*) AS txn_count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY issuer_bank), 2) AS share_of_bank_failures_pct,
    ROUND(SUM(amount_inr), 2) AS impacted_gmv_inr,
    ROUND(AVG(latency_ms), 0) AS avg_latency_ms
FROM transactions
WHERE status IN ('FAILED', 'PENDING')
GROUP BY issuer_bank, failure_reason
ORDER BY issuer_bank, impacted_gmv_inr DESC;

-- hourly funnel and peak-hour (18:00-21:00) behavior
WITH hourly_stats AS (
    SELECT
        HOUR(txn_timestamp) AS txn_hour,
        COUNT(*) AS total_txns,
        SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_txns,
        SUM(CASE WHEN status = 'FAILED' AND failure_reason = 'BANK_TIMEOUT' THEN 1 ELSE 0 END) AS timeout_txns,
        ROUND(SUM(CASE WHEN status IN ('FAILED', 'PENDING') THEN amount_inr ELSE 0 END), 2) AS lost_gmv_inr,
        ROUND(AVG(latency_ms), 0) AS avg_latency_ms
    FROM transactions
    GROUP BY HOUR(txn_timestamp)
)
SELECT
    txn_hour,
    CASE WHEN txn_hour BETWEEN 18 AND 21 THEN 'PEAK' ELSE 'OFF_PEAK' END AS traffic_window,
    total_txns,
    failed_txns,
    timeout_txns,
    ROUND(100.0 * failed_txns / NULLIF(total_txns, 0), 2) AS failure_rate_pct,
    lost_gmv_inr,
    avg_latency_ms
FROM hourly_stats
ORDER BY txn_hour;