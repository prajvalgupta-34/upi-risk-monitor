# UPI Payment Operations & Fraud Risk Monitor

An end-to-end FinTech payment funnel and fraud-risk monitoring system built with **MySQL 8.0**, **Python**, **Scikit-learn**, and **Streamlit**. Simulates **50,000 UPI transactions across 1,000 accounts** over 30 days to diagnose issuer-bank failures and peak-hour Lost GMV, score rolling-window fraud risk, and demonstrate interactive risk-ops controls.

> **Data & Scope Disclosure:** All transaction records, bank failure rates, and fraud labels are synthetically generated (3.00% injected fraud rate) for portfolio demonstration. RBAC controls in the dashboard simulate role permissions for demo purposes rather than production authentication.

## Architecture Overview

1. **Normalized Schema & Views (`db/schema.sql`, `db/payment_funnel.sql`, `db/risk_engine.sql`)**
   - Relational schema with `admin_roles`, `users`, `transactions`, and `risk_events`, indexed on `(user_id, txn_timestamp)` for rolling time-window analytics.
   - Uses MySQL window functions (`RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING AND CURRENT ROW` and `LAG()`) to compute rolling velocity, rolling spend, and inter-transaction intervals.
2. **Data Seeding, Hybrid Scoring & SHA-256 Audit Chain (`scripts/seed_data.py`, `scripts/build_risk_events.py`)**
   - Generates 50,000 transactions with issuer-bank failure profiles, evening peak-hour pressure (`18:00–21:00`), legitimate burst shoppers (for realistic false-positive trade-offs), and injected account-drain fraud.
   - Combines deterministic SQL risk rules (75%) with an unsupervised `IsolationForest` anomaly score (25%), achieving **0.9605 PR-AUC** at threshold `>= 66` and logging 1,789 elevated events (`score >= 36`) in a SHA-256 hash-chained audit log.
   - Exports a lightweight SQLite snapshot (`data/upi_demo.db`) and evaluation summary (`data/model_metrics.json`) so the app can run both locally against MySQL and on cloud hosts without a local database.
3. **Interactive Risk Ops Dashboard (`app.py`)**
   - **Tab 1 — Payment Operations & Funnel:** Tracks total transactions, success rate, Lost GMV (failed + pending rupee volume), average gateway latency, bank-wise failure rates, hourly peak-window trends, and failure-reason breakdowns.
   - **Tab 2 — Fraud Ops & Risk Rising Feed:** Compares SQL Rules, IsolationForest, and Hybrid variants (`Precision`, `Recall`, `F1`, `PR-AUC`), provides a What-If auto-block threshold simulator (`Simulated Fraud Prevented`, `Fraud Value Capture`, `False Positive Rate`, `Blocked Legit GMV`), displays escalating accounts and zoomed burst timelines, and supports RBAC-gated account actions with SHA-256 chain verification.

## Project Structure

```text
upi-risk-monitor/
├── db/
│   ├── schema.sql
│   ├── payment_funnel.sql
│   └── risk_engine.sql
├── scripts/
│   ├── db.py
│   ├── seed_data.py
│   └── build_risk_events.py
├── tests/
│   └── test_pipeline.py
├── data/
│   ├── upi_demo.db
│   └── model_metrics.json
├── app.py
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md