"""
tools.py
--------
Defines the actions the agent is allowed to take against "the organization
database". Each tool is a narrow, parameterized, READ-ONLY function - not
a free-form SQL generator.

Why not let the LLM write arbitrary SQL against a bank DB?
  - It can hallucinate destructive or overly broad queries.
  - A malicious or malformed question (prompt injection via a support
    ticket, a pasted email, etc.) could trick it into writing something
    that leaks more than intended.
  - Scoped tools are auditable: you know exactly what data any tool call
    *could* return, which is what a bank's compliance/security review
    will want to see.

`diagnose_account_issue` is the main "why isn't this working" tool: it
checks account status, card status, KYC status, daily transfer limit
usage, recent failed transactions, and open complaints in ONE call, so
the employee/agent doesn't have to chain five separate lookups for a
common "my transfer failed" / "my account isn't working" question.

An optional, clearly-labeled `run_readonly_sql` tool is included at the
bottom for prototyping flexibility. Keep it OFF (see agent.py) for
anything beyond your own local experimentation.
"""

import re
import sqlite3
from typing import Optional, List, Dict, Any

from langchain_core.tools import tool
from database import get_connection


def _rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


@tool
def search_customer(query: str) -> List[Dict[str, Any]]:
    """Look up customers by name, phone number, email, or customer ID.
    Use this first when the employee doesn't already have a customer_id.
    Returns basic profile info (no account balances)."""
    conn = get_connection(read_only=True)
    like = f"%{query.strip()}%"
    rows = conn.execute(
        """SELECT customer_id, full_name, phone, email, kyc_status, branch, date_joined
           FROM customers
           WHERE customer_id LIKE ? OR full_name LIKE ? OR phone LIKE ? OR email LIKE ?""",
        (like, like, like, like),
    ).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


@tool
def get_customer_profile(customer_id: str) -> Dict[str, Any]:
    """Get full profile details for a specific customer_id (e.g. 'CUST-1001')."""
    conn = get_connection(read_only=True)
    row = conn.execute(
        "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {"error": f"No customer found with id {customer_id}"}


@tool
def get_accounts_for_customer(customer_id: str) -> List[Dict[str, Any]]:
    """List all accounts (savings, current, FD, etc.) belonging to a customer_id,
    including balance, status, and card status."""
    conn = get_connection(read_only=True)
    rows = conn.execute(
        "SELECT * FROM accounts WHERE customer_id = ?", (customer_id,)
    ).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


@tool
def get_account_details(account_id: str) -> Dict[str, Any]:
    """Get details (balance, status, card status, transfer limits) for a
    specific account_id (e.g. 'ACC-5001')."""
    conn = get_connection(read_only=True)
    row = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {"error": f"No account found with id {account_id}"}


@tool
def get_recent_transactions(account_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Get the most recent transactions for a given account_id, newest first,
    including status and failure_reason for any failed ones.
    `limit` caps how many rows come back (default 10, max 100)."""
    limit = max(1, min(int(limit), 100))
    conn = get_connection(read_only=True)
    rows = conn.execute(
        """SELECT * FROM transactions WHERE account_id = ?
           ORDER BY txn_date DESC LIMIT ?""",
        (account_id, limit),
    ).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


@tool
def get_complaints(customer_id: Optional[str] = None, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get complaint / dispute tickets, filtered by customer_id and/or account_id.
    Provide at least one of the two."""
    if not customer_id and not account_id:
        return [{"error": "Provide a customer_id or account_id to look up complaints."}]
    conn = get_connection(read_only=True)
    query = "SELECT * FROM complaints WHERE 1=1"
    params: List[str] = []
    if customer_id:
        query += " AND customer_id = ?"
        params.append(customer_id)
    if account_id:
        query += " AND account_id = ?"
        params.append(account_id)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return _rows_to_dicts(rows)


@tool
def diagnose_account_issue(account_id: str) -> Dict[str, Any]:
    """USE THIS for 'my account isn't working', 'my money isn't transferring',
    'my transaction failed', 'why can't I withdraw/transfer' type questions.

    Runs every common check in one shot against a given account_id:
    account status (frozen/blocked/dormant), card status, the customer's
    KYC status, today's transfer-limit usage, the most recent transactions
    (flagging any Failed/Pending ones with their reason), and any open
    complaints tied to the account. Returns the raw findings plus a
    'likely_causes' list so the answer can be given without a follow-up
    lookup.
    """
    conn = get_connection(read_only=True)

    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if not account:
        conn.close()
        return {"error": f"No account found with id {account_id}"}
    account = dict(account)

    customer = conn.execute(
        "SELECT * FROM customers WHERE customer_id = ?", (account["customer_id"],)
    ).fetchone()
    customer = dict(customer) if customer else {}

    recent_txns = conn.execute(
        """SELECT * FROM transactions WHERE account_id = ?
           ORDER BY txn_date DESC LIMIT 5""",
        (account_id,),
    ).fetchall()
    recent_txns = _rows_to_dicts(recent_txns)

    open_complaints = conn.execute(
        """SELECT * FROM complaints WHERE account_id = ? AND status != 'Resolved'""",
        (account_id,),
    ).fetchall()
    open_complaints = _rows_to_dicts(open_complaints)

    conn.close()

    likely_causes = []

    if account["status"] != "Active":
        likely_causes.append(
            f"Account status is '{account['status']}'"
            + (f": {account['status_reason']}" if account.get("status_reason") else "")
        )

    if account.get("card_status") == "Blocked":
        likely_causes.append("Debit/ATM card is Blocked")
    elif account.get("card_status") == "Expired":
        likely_causes.append("Debit/ATM card is Expired")

    if customer.get("kyc_status") and customer["kyc_status"] != "Verified":
        likely_causes.append(f"Customer KYC status is '{customer['kyc_status']}' (can block outward transfers)")

    limit = account.get("daily_transfer_limit") or 0
    used = account.get("used_today") or 0
    if limit and used >= limit:
        likely_causes.append(f"Daily transfer limit reached ({used} of {limit} used today)")

    failed_or_pending = [t for t in recent_txns if t["status"] in ("Failed", "Pending")]
    for t in failed_or_pending:
        if t["status"] == "Failed":
            likely_causes.append(
                f"Recent failed transaction on {t['txn_date']} ({t['description']}): {t.get('failure_reason') or 'no reason logged'}"
            )
        else:
            likely_causes.append(
                f"Recent transaction on {t['txn_date']} ({t['description']}) is still Pending"
            )

    if open_complaints:
        likely_causes.append(f"{len(open_complaints)} open complaint(s) already on file for this account")

    if not likely_causes:
        likely_causes.append("No obvious issues found - account, card, KYC, and limits all look normal. Recommend asking the customer for more specifics (exact error message, time of attempt, channel used).")

    return {
        "account": account,
        "customer_kyc_status": customer.get("kyc_status"),
        "recent_transactions": recent_txns,
        "open_complaints": open_complaints,
        "likely_causes": likely_causes,
    }


# ---------------------------------------------------------------------------
# OPTIONAL / HIGHER-RISK: free-form read-only SQL tool.
# Off by default in agent.py. Useful for local prototyping only.
# ---------------------------------------------------------------------------
_ALLOWED_PREFIX = re.compile(r"^\s*SELECT\s", re.IGNORECASE)
_BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA|CREATE|REPLACE|VACUUM)\b",
    re.IGNORECASE,
)


@tool
def run_readonly_sql(sql_query: str) -> Any:
    """[PROTOTYPE ONLY - keep disabled in anything resembling production]
    Run a single read-only SELECT statement against the database and
    return the rows. Any non-SELECT statement is rejected."""
    if not _ALLOWED_PREFIX.match(sql_query) or _BLOCKED_KEYWORDS.search(sql_query) or ";" in sql_query.strip().rstrip(";"):
        return {"error": "Only a single SELECT statement is permitted."}
    conn = get_connection(read_only=True)
    try:
        rows = conn.execute(sql_query).fetchall()
        return _rows_to_dicts(rows)
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()


SAFE_TOOLS = [
    search_customer,
    get_customer_profile,
    get_accounts_for_customer,
    get_account_details,
    get_recent_transactions,
    get_complaints,
    diagnose_account_issue,
]

ALL_TOOLS_INCLUDING_RAW_SQL = SAFE_TOOLS + [run_readonly_sql]
