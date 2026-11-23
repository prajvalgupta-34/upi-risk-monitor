import random
from datetime import datetime, timedelta
import numpy as np
from db import get_connection

random.seed(42)
np.random.seed(42)

BANKS = ['SBI', 'HDFC', 'ICICI', 'AXIS', 'KOTAK', 'PNB']
BANK_WEIGHTS = [0.28, 0.22, 0.18, 0.14, 0.10, 0.08]
BANK_HANDLES = {'SBI': 'oksbi', 'HDFC': 'okhdfc', 'ICICI': 'okicici', 'AXIS': 'okaxis', 'KOTAK': 'kotak', 'PNB': 'okpnb'}

# realistic base failure rates (SBI/PNB have slightly higher peak timeout rates)
BANK_FAIL_PROB = {'SBI': 0.085, 'PNB': 0.09, 'AXIS': 0.055, 'KOTAK': 0.05, 'ICICI': 0.04, 'HDFC': 0.035}

CATEGORIES = ['P2P', 'GROCERY', 'UTILITIES', 'ELECTRONICS', 'TRAVEL', 'GAMING', 'CRYPTO_P2P']
CAT_WEIGHTS = [0.38, 0.24, 0.14, 0.09, 0.07, 0.05, 0.03]
CITIES = ['Delhi', 'Mumbai', 'Bengaluru', 'Hyderabad', 'Pune', 'Chennai', 'Kolkata', 'Jaipur']

FIRST_NAMES = ['Aarav', 'Rohan', 'Priya', 'Ananya', 'Kabir', 'Sneha', 'Vikram', 'Neha', 'Arjun', 'Pooja', 'Siddharth', 'Kavya']
LAST_NAMES = ['Sharma', 'Verma', 'Gupta', 'Patel', 'Singh', 'Nair', 'Reddy', 'Mehta', 'Iyer', 'Joshi']


def generate_users(n=1000):
    users = []
    base_date = datetime(2025, 1, 1)
    for i in range(1, n + 1):
        fname = random.choice(FIRST_NAMES)
        lname = random.choice(LAST_NAMES)
        bank = np.random.choice(BANKS, p=BANK_WEIGHTS)
        vpa = f"{fname.lower()}.{lname.lower()}{i}@{BANK_HANDLES[bank]}"
        kyc = 'FULL_KYC' if random.random() < 0.85 else 'MIN_KYC'
        avg_spend = round(float(np.random.lognormal(mean=9.4, sigma=0.6)), 2) # ~₹12k median
        created_at = base_date + timedelta(days=random.randint(0, 500))
        users.append((f"{fname} {lname}", vpa, bank, kyc, 'ACTIVE', avg_spend, created_at))
    return users


def generate_transactions(n_txns=50000, n_users=1000, user_profiles=None):
    txns = []
    start_time = (datetime.now() - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    rrn_counter = 630000000000 # 12-digit NPCI-style RRN

    fraud_users = set(random.sample(range(1, n_users + 1), 60))
    remaining_users = [u for u in range(1, n_users + 1) if u not in fraud_users]
    legit_burst_users = random.sample(remaining_users, 50)

    # 1. baseline normal traffic (48,150 rows)
    normal_count = n_txns - (len(fraud_users) * 25) - (len(legit_burst_users) * 7)
    for _ in range(normal_count):
        rrn_counter += 1
        uid = random.randint(1, n_users)
        bank, avg_spend = user_profiles[uid]
        cat = np.random.choice(CATEGORIES, p=CAT_WEIGHTS)

        day_offset = random.randint(0, 29)
        hour = int(np.random.choice(range(24), p=[
            0.01, 0.01, 0.01, 0.01, 0.01, 0.02, 0.03, 0.04,
            0.05, 0.06, 0.06, 0.06, 0.06, 0.05, 0.05, 0.05,
            0.06, 0.07, 0.08, 0.08, 0.06, 0.04, 0.02, 0.01
        ]))
        ts = start_time + timedelta(days=day_offset, hours=hour, minutes=random.randint(0, 59), seconds=random.randint(0, 59))

        amount = round(max(10.0, float(np.random.lognormal(mean=6.0, sigma=1.1))), 2)
        is_peak = 18 <= hour <= 21
        fail_prob = BANK_FAIL_PROB[bank] * (1.6 if is_peak else 1.0)

        roll = random.random()
        if roll < fail_prob:
            status = 'FAILED'
            reason = np.random.choice(
                ['BANK_TIMEOUT', 'INSUFFICIENT_FUNDS', 'UPI_PIN_ERROR', 'NPCI_SWITCH_DOWN'],
                p=[0.50, 0.25, 0.15, 0.10]
            )
            latency = random.randint(2500, 6000) if reason == 'BANK_TIMEOUT' else random.randint(400, 1500)
        elif roll < fail_prob + 0.01:
            status, reason, latency = 'PENDING', 'BANK_TIMEOUT', random.randint(3000, 5500)
        else:
            status, reason, latency = 'SUCCESS', 'NONE', random.randint(180, 950)

        txns.append((
            str(rrn_counter), uid, cat, bank, amount, ts, status, reason,
            latency, f"DEV_{uid % 800}", random.choice(CITIES), 0
        ))

    # 2. legitimate bursty shoppers (350 rows) — creates realistic false-positive trade-offs
    for uid in legit_burst_users:
        bank, avg_spend = user_profiles[uid]
        spree_start = start_time + timedelta(days=random.randint(1, 28), hours=random.randint(10, 21))
        user_city = random.choice(CITIES)

        for step in range(7):
            rrn_counter += 1
            ts = spree_start + timedelta(seconds=step * random.randint(30, 100))
            amount = round(avg_spend * random.uniform(0.12, 0.45), 2)
            cat = random.choice(['ELECTRONICS', 'TRAVEL', 'GROCERY', 'P2P'])
            txns.append((
                str(rrn_counter), uid, cat, bank, amount, ts, 'SUCCESS', 'NONE',
                random.randint(200, 900), f"DEV_{uid % 800}", user_city, 0
            ))

    # 3. injected fraud patterns (1,500 rows across 60 compromised users)
    for uid in fraud_users:
        bank, avg_spend = user_profiles[uid]
        burst_start = start_time + timedelta(days=random.randint(1, 28), hours=random.randint(0, 4))
        fraud_device = f"DEV_NEW_{random.randint(9000, 9999)}"
        fraud_city = random.choice(CITIES)

        for step in range(25):
            rrn_counter += 1
            ts = burst_start + timedelta(seconds=step * random.randint(15, 45))
            # small probe txns first, then high-value drain
            amount = round(random.uniform(50, 200), 2) if step < 3 else round(avg_spend * random.uniform(0.4, 1.8), 2)
            cat = random.choice(['CRYPTO_P2P', 'GAMING', 'ELECTRONICS', 'P2P'])
            txns.append((
                str(rrn_counter), uid, cat, bank, amount, ts, 'SUCCESS', 'NONE',
                random.randint(200, 800), fraud_device, fraud_city, 1
            ))

    random.shuffle(txns)
    return txns


def main():
    conn = get_connection()
    cur = conn.cursor()

    # clear existing synthetic data cleanly before reloading
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    cur.execute("TRUNCATE TABLE risk_events")
    cur.execute("TRUNCATE TABLE transactions")
    cur.execute("TRUNCATE TABLE users")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    conn.commit()

    print("Seeding users...")
    users = generate_users(1000)
    cur.executemany("""
        INSERT INTO users (full_name, vpa_handle, primary_bank, kyc_tier, account_status, avg_monthly_spend, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, users)
    conn.commit()

    cur.execute("SELECT user_id, primary_bank, avg_monthly_spend FROM users")
    user_profiles = {row[0]: (row[1], float(row[2])) for row in cur.fetchall()}

    print("Generating 50,000 UPI transactions...")
    txns = generate_transactions(50000, 1000, user_profiles)

    insert_sql = """
        INSERT INTO transactions (
            upi_rrn, user_id, merchant_category, issuer_bank, amount_inr,
            txn_timestamp, status, failure_reason, latency_ms, device_id, ip_city, is_fraud
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    batch_size = 5000
    for i in range(0, len(txns), batch_size):
        cur.executemany(insert_sql, txns[i:i + batch_size])
        conn.commit()
        print(f"Inserted {min(i + batch_size, len(txns))} / {len(txns)} rows")

    cur.close()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()