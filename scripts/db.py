import os
import sqlite3
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = os.path.join("data", "upi_demo.db")


def get_connection():
    """Returns (conn, backend_type) where backend_type is 'mysql' or 'sqlite'."""
    use_sqlite = os.getenv("USE_SQLITE", "false").lower() == "true"

    if not use_sqlite:
        try:
            conn = mysql.connector.connect(
                host=os.getenv("DB_HOST", "localhost"),
                user=os.getenv("DB_USER", "root"),
                password=os.getenv("DB_PASSWORD", ""),
                database=os.getenv("DB_NAME", "upi_risk_monitor"),
                connection_timeout=3,
            )
            return conn, "mysql"
        except Exception:
            pass

    if os.path.exists(SQLITE_PATH):
        conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        return conn, "sqlite"

    raise RuntimeError(
        "Could not connect to MySQL and data/upi_demo.db was not found. "
        "Run scripts/build_risk_events.py first."
    )