"""
search_engine.py
-----------------
Fast path for "just answer my question" queries.

Instead of an LLM deciding, one tool call at a time, which lookups to
run (several round trips to the model), this module does ALL the
relevant lookups in a single pass using plain Python + SQL:

  1. Pull out any explicit IDs in the question (CUST-###, ACC-###, etc.)
  2. If a name/phone/email is mentioned instead, find the matching
     customer(s).
  3. Once we know the customer/account(s) involved, pull their profile,
     accounts, recent transactions, and complaints in one go.
  4. Also do a keyword search over transaction/complaint descriptions,
     in case the question is about *content* ("duplicate charge",
     "frozen account") rather than an explicit ID.

The result is a compact bundle of already-fetched rows. agent.py then
makes exactly ONE LLM call to turn that bundle into a natural-language
answer - instead of N round trips. That's the main speed win.

If nothing relevant is found at all, the caller should fall back to the
slower multi-step tool-calling agent for a more open-ended search.
"""

import re
from typing import Dict, Any, List, Set

from database import get_connection

ID_PATTERNS = {
    "customer_id": re.compile(r"\bCUST-\d+\b", re.IGNORECASE),
    "account_id": re.compile(r"\bACC-\d+\b", re.IGNORECASE),
    "transaction_id": re.compile(r"\bTXN-\d+\b", re.IGNORECASE),
    "complaint_id": re.compile(r"\bCMP-\d+\b", re.IGNORECASE),
}
PHONE_PATTERN = re.compile(r"\b\d{10}\b")
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

STOPWORDS = {
    "what", "whats", "what's", "is", "the", "of", "for", "on", "show", "me",
    "find", "customer", "account", "does", "has", "have", "why", "was",
    "recent", "please", "can", "you", "tell", "about", "check", "look",
    "up", "give", "info", "information", "balance", "status", "details",
    "any", "there", "a", "an", "and", "to", "with", "in", "his", "her",
    "their", "get",
}


def _rows(cur_rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur_rows]


def _extract_ids(question: str) -> Dict[str, List[str]]:
    found = {}
    for key, pattern in ID_PATTERNS.items():
        matches = [m.upper() for m in pattern.findall(question)]
        if matches:
            found[key] = list(dict.fromkeys(matches))  # de-dupe, keep order
    return found


def _extract_keywords(question: str) -> List[str]:
    words = re.findall(r"[A-Za-z0-9]+", question.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def gather_context(question: str, max_customers: int = 3, max_rows_per_table: int = 8) -> Dict[str, Any]:
    """Single-pass lookup. Returns a dict bundle of whatever rows are
    relevant to the question. Empty dict (all lists empty) means the
    fast path found nothing and the caller should fall back to the
    slower agent."""
    conn = get_connection(read_only=True)
    context: Dict[str, Any] = {
        "customers": [], "accounts": [], "transactions": [], "complaints": [],
    }
    seen_customer_ids: Set[str] = set()
    seen_account_ids: Set[str] = set()

    ids = _extract_ids(question)
    phone = PHONE_PATTERN.search(question)
    email = EMAIL_PATTERN.search(question)

    # 1. Explicit customer IDs
    for cid in ids.get("customer_id", []):
        row = conn.execute("SELECT * FROM customers WHERE customer_id = ?", (cid,)).fetchone()
        if row:
            context["customers"].append(dict(row))
            seen_customer_ids.add(cid)

    # 2. Explicit account IDs -> also pull the owning customer
    for aid in ids.get("account_id", []):
        row = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (aid,)).fetchone()
        if row:
            context["accounts"].append(dict(row))
            seen_account_ids.add(aid)
            if row["customer_id"] not in seen_customer_ids:
                crow = conn.execute(
                    "SELECT * FROM customers WHERE customer_id = ?", (row["customer_id"],)
                ).fetchone()
                if crow:
                    context["customers"].append(dict(crow))
                    seen_customer_ids.add(row["customer_id"])

    # 3. Explicit transaction IDs
    for tid in ids.get("transaction_id", []):
        row = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", (tid,)).fetchone()
        if row:
            context["transactions"].append(dict(row))
            seen_account_ids.add(row["account_id"])

    # 4. Explicit complaint IDs
    for cmid in ids.get("complaint_id", []):
        row = conn.execute("SELECT * FROM complaints WHERE complaint_id = ?", (cmid,)).fetchone()
        if row:
            context["complaints"].append(dict(row))
            if row["customer_id"]:
                seen_customer_ids.add(row["customer_id"])

    # 5. Phone / email match -> find customer
    if phone or email:
        term = phone.group() if phone else email.group()
        rows = conn.execute(
            "SELECT * FROM customers WHERE phone = ? OR email = ?", (term, term)
        ).fetchall()
        for r in rows:
            if r["customer_id"] not in seen_customer_ids:
                context["customers"].append(dict(r))
                seen_customer_ids.add(r["customer_id"])

    # 6. No IDs found at all -> try matching a name, and try keyword search
    #    over transaction/complaint descriptions (e.g. "duplicate charge").
    if not seen_customer_ids and not seen_account_ids and not context["transactions"] and not context["complaints"]:
        keywords = _extract_keywords(question)
        if keywords:
            # Name search: try the longest keywords first as they're most
            # likely to be a surname/distinctive name.
            for kw in sorted(keywords, key=len, reverse=True)[:3]:
                like = f"%{kw}%"
                rows = conn.execute(
                    "SELECT * FROM customers WHERE full_name LIKE ? LIMIT ?",
                    (like, max_customers),
                ).fetchall()
                for r in rows:
                    if r["customer_id"] not in seen_customer_ids:
                        context["customers"].append(dict(r))
                        seen_customer_ids.add(r["customer_id"])

            # Content search: transactions and complaints whose description
            # or category mentions any of the keywords.
            like_clauses = " OR ".join(["description LIKE ?"] * len(keywords))
            params = [f"%{kw}%" for kw in keywords]
            txn_rows = conn.execute(
                f"SELECT * FROM transactions WHERE {like_clauses} "
                f"ORDER BY txn_date DESC LIMIT ?",
                params + [max_rows_per_table],
            ).fetchall()
            context["transactions"].extend(_rows(txn_rows))
            for r in txn_rows:
                seen_account_ids.add(r["account_id"])

            cmp_like_clauses = " OR ".join(
                ["description LIKE ?"] * len(keywords) + ["category LIKE ?"] * len(keywords)
            )
            cmp_params = [f"%{kw}%" for kw in keywords] * 2
            cmp_rows = conn.execute(
                f"SELECT * FROM complaints WHERE {cmp_like_clauses} LIMIT ?",
                cmp_params + [max_rows_per_table],
            ).fetchall()
            context["complaints"].extend(_rows(cmp_rows))
            for r in cmp_rows:
                if r["customer_id"]:
                    seen_customer_ids.add(r["customer_id"])

    # 7. Expand: for every customer we now know about, pull their accounts
    #    (unless we already have specific accounts from an explicit ID).
    for cid in seen_customer_ids:
        acct_rows = conn.execute(
            "SELECT * FROM accounts WHERE customer_id = ?", (cid,)
        ).fetchall()
        for r in acct_rows:
            if r["account_id"] not in seen_account_ids:
                context["accounts"].append(dict(r))
                seen_account_ids.add(r["account_id"])

    # 8. For every account now in scope, pull recent transactions and
    #    complaints (unless we already fetched something specific).
    if not context["transactions"]:
        for aid in list(seen_account_ids)[:max_customers]:
            txn_rows = conn.execute(
                "SELECT * FROM transactions WHERE account_id = ? "
                "ORDER BY txn_date DESC LIMIT ?",
                (aid, 5),
            ).fetchall()
            context["transactions"].extend(_rows(txn_rows))

    if not context["complaints"]:
        for cid in list(seen_customer_ids)[:max_customers]:
            cmp_rows = conn.execute(
                "SELECT * FROM complaints WHERE customer_id = ?", (cid,)
            ).fetchall()
            context["complaints"].extend(_rows(cmp_rows))

    conn.close()

    is_empty = not any(context.values())
    context["_found_anything"] = not is_empty
    return context
