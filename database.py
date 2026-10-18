"""
database.py
------------
Creates and seeds a small SQLite database that stands in for the bank's
"organization database" for this prototype.

In a real deployment you would NOT create/seed a database like this.
Instead, `tools.py` would connect to a read-only replica / read-only
service account on the real core-banking database. Swap out DB_PATH /
get_connection() there when you're ready to point this at something real.

Schema is intentionally widened to cover the account problems employees
hear about most often: account frozen/blocked/dormant, card blocked,
KYC not verified, daily transfer limit hit, and failed transactions with
a logged reason.
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "bank_demo.db")


def get_connection(read_only: bool = False) -> sqlite3.Connection:
    """Return a connection to the demo DB.

    read_only=True opens the file in SQLite's read-only URI mode, so even
    a bug in the app code can't accidentally write to the database. Tools
    that only ever SELECT should use read_only=True.
    """
    if read_only:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(force: bool = False) -> None:
    """Create tables and seed sample data. Safe to call multiple times."""
    if os.path.exists(DB_PATH) and not force:
        return
    if os.path.exists(DB_PATH) and force:
        os.remove(DB_PATH)

    conn = get_connection(read_only=False)
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE customers (
            customer_id   TEXT PRIMARY KEY,
            full_name     TEXT NOT NULL,
            phone         TEXT,
            email         TEXT,
            kyc_status    TEXT,       -- Verified | Pending | Rejected
            branch        TEXT,
            date_joined   TEXT
        );

        CREATE TABLE accounts (
            account_id            TEXT PRIMARY KEY,
            customer_id           TEXT NOT NULL,
            account_type          TEXT,
            balance                REAL,
            status                 TEXT,   -- Active | Frozen | Blocked | Dormant | Closed
            status_reason          TEXT,   -- why, if not Active
            card_status            TEXT,   -- Active | Blocked | Expired | Not Issued
            daily_transfer_limit   REAL,
            used_today             REAL,
            opened_date            TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
        );

        CREATE TABLE transactions (
            transaction_id TEXT PRIMARY KEY,
            account_id     TEXT NOT NULL,
            txn_date       TEXT,
            txn_type       TEXT,    -- Debit | Credit | Transfer
            amount         REAL,
            description    TEXT,
            status         TEXT,    -- Success | Failed | Pending | Reversed
            failure_reason TEXT,    -- populated when status = Failed
            FOREIGN KEY (account_id) REFERENCES accounts(account_id)
        );

        CREATE TABLE complaints (
            complaint_id  TEXT PRIMARY KEY,
            customer_id   TEXT NOT NULL,
            account_id    TEXT,
            date_filed    TEXT,
            category      TEXT,
            description   TEXT,
            status        TEXT,
            resolution    TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
        );
        """
    )

    customers = [
        ("CUST-1001", "Ritika Sharma", "9800011122", "ritika.sharma@example.com", "Verified", "Asansol Main", "2019-03-11"),
        ("CUST-1002", "Manoj Kundu", "9800022233", "manoj.kundu@example.com", "Verified", "Asansol Main", "2021-07-02"),
        ("CUST-1003", "Priya Banerjee", "9800033344", "priya.b@example.com", "Pending", "Durgapur Branch", "2023-01-19"),
        ("CUST-1004", "Arjun Dutta", "9800044455", "arjun.dutta@example.com", "Pending", "Asansol Main", "2024-11-05"),
    ]
    cur.executemany(
        "INSERT INTO customers VALUES (?,?,?,?,?,?,?)", customers
    )

    accounts = [
        # account_id, customer_id, type, balance, status, status_reason, card_status, daily_limit, used_today, opened
        ("ACC-5001", "CUST-1001", "Savings", 84250.50, "Active", None, "Active", 100000, 20000, "2019-03-12"),
        ("ACC-5002", "CUST-1001", "Fixed Deposit", 200000.00, "Active", None, "Not Issued", 0, 0, "2022-05-01"),
        ("ACC-5003", "CUST-1002", "Savings", 1520.00, "Active", None, "Active", 25000, 0, "2021-07-03"),
        ("ACC-5004", "CUST-1003", "Current", -450.00, "Frozen", "Cheque bounce - account frozen pending fee settlement", "Blocked", 0, 0, "2023-01-20"),
        ("ACC-5006", "CUST-1002", "Savings", 45000.00, "Active", None, "Active", 10000, 10000, "2023-02-14"),
        ("ACC-5005", "CUST-1004", "Savings", 15000.00, "Active", None, "Active", 50000, 0, "2024-11-06"),
        ("ACC-5007", "CUST-1001", "Savings", 3000.00, "Active", None, "Blocked", 50000, 0, "2024-01-10"),
    ]
    cur.executemany(
        "INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?)", accounts
    )

    transactions = [
        # transaction_id, account_id, date, type, amount, description, status, failure_reason
        ("TXN-9001", "ACC-5001", "2025-09-01", "Debit", 2500.00, "POS purchase - Grocery Mart", "Success", None),
        ("TXN-9002", "ACC-5001", "2025-09-05", "Credit", 50000.00, "Salary credit", "Success", None),
        ("TXN-9003", "ACC-5003", "2025-09-10", "Debit", 5000.00, "ATM withdrawal", "Success", None),
        ("TXN-9004", "ACC-5003", "2025-09-11", "Debit", 5000.00, "ATM withdrawal (duplicate?)", "Success", None),
        ("TXN-9005", "ACC-5004", "2025-09-12", "Debit", 450.00, "Cheque bounce fee", "Success", None),
        ("TXN-9006", "ACC-5005", "2025-09-14", "Transfer", 5000.00, "NEFT to external account", "Failed", "KYC re-verification pending - outward transfers blocked until KYC is completed"),
        ("TXN-9007", "ACC-5006", "2025-09-15", "Transfer", 2000.00, "UPI transfer to merchant", "Failed", "Daily transfer limit of 10,000 already used for today"),
        ("TXN-9008", "ACC-5007", "2025-09-16", "Debit", 500.00, "POS purchase attempt", "Failed", "Card is blocked (reported lost by customer on 2025-09-15)"),
        ("TXN-9009", "ACC-5003", "2025-09-16", "Transfer", 3000.00, "IMPS transfer to family member", "Failed", "Insufficient balance - account balance 1,520 is less than transfer amount"),
        ("TXN-9010", "ACC-5001", "2025-09-17", "Transfer", 15000.00, "NEFT rent payment", "Pending", None),
    ]
    cur.executemany(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", transactions
    )

    complaints = [
        ("CMP-01", "CUST-1002", "ACC-5003", "2025-09-11", "Duplicate Transaction",
         "Customer reports being charged twice for a single ATM withdrawal of 5000.",
         "Open", None),
        ("CMP-02", "CUST-1003", "ACC-5004", "2025-09-12", "Account Frozen",
         "Customer disputes account freeze and wants clarification on the cheque bounce fee.",
         "In Progress", None),
        ("CMP-03", "CUST-1001", "ACC-5007", "2025-09-16", "Card Blocked",
         "Customer reported card lost and wants a replacement issued.",
         "Open", None),
    ]
    cur.executemany(
        "INSERT INTO complaints VALUES (?,?,?,?,?,?,?,?)", complaints
    )

    conn.commit()
    conn.close()
    print(f"Demo database created at {DB_PATH}")


if __name__ == "__main__":
    init_db(force=True)
