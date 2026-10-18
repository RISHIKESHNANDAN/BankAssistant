# Bank Employee Assist — Prototype

An agent for bank branch employees: type a question about a customer or
their account — including "it's not working" style complaints — and get
a fast, grounded answer pulled from the organization's database.

## Why it's fast

Most agent demos make the LLM decide, one tool call at a time, which
lookups to run — that's several model round trips before you get an
answer. This one does the opposite by default:

1. **Fast path (`search_engine.py` + `quick_answer` in `agent.py`)** —
   plain Python + SQL pulls every relevant row (customer, account(s),
   recent transactions, complaints) in a single pass, using IDs, phone/
   email, or name/keywords found in the question. Then **exactly one**
   LLM call turns that bundle into an answer. This is what handles the
   vast majority of questions, including diagnosing "why isn't this
   working."
2. **Fallback (`ask_agent`, a LangGraph tool-calling agent)** — only
   runs if the fast path finds nothing (e.g. a vague question with no
   name/ID/keyword match). Slower, but more exploratory: it can chain
   lookups (find the customer → their accounts → that account's
   transactions) on its own.

The UI (`app.py`) tries the fast path first and only falls back when needed.

## Files

- `database.py` — seeds a local SQLite DB (`bank_demo.db`) standing in
  for "the organization's database." Tables: `customers`, `accounts`
  (now including `status_reason`, `card_status`, `daily_transfer_limit`,
  `used_today`), `transactions` (now including `failure_reason`),
  `complaints`.
- `search_engine.py` — the fast-path lookup described above.
- `tools.py` — scoped, read-only tools for the fallback agent, including
  `diagnose_account_issue`, which checks account status, card status,
  KYC status, daily transfer limit, recent failed/pending transactions,
  and open complaints in one call.
- `agent.py` — `quick_answer()` (fast path), `ask_agent()` (fallback),
  and the LLM provider factory (OpenAI / Azure OpenAI / Groq). Both
  paths write to a CSV audit log.
- `app.py` — Streamlit chat UI.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # then add your API key
```

## Run

```bash
streamlit run app.py
```

The demo database is created automatically on first run.

### Troubleshooting scenarios seeded in the demo data

These map to the common "my account doesn't work" complaints:

| Try asking about... | What's wrong (in the demo data) |
|---|---|
| `ACC-5005` | KYC re-verification pending — outward transfers blocked |
| `ACC-5006` | Daily transfer limit already used up for today |
| `ACC-5007` | Card blocked (reported lost) |
| `ACC-5003` | Insufficient balance for the attempted transfer |
| `ACC-5004` | Account frozen (cheque bounce) + card blocked + KYC pending |
| `ACC-5001` | Nothing wrong — one transfer still Pending, otherwise healthy |

Ask things like *"customer says their money isn't transferring, account
ACC-5006"* and the answer should name the actual blocker (limit reached)
plus the numbers, in one turn.

Every answer has an expandable panel showing exactly what data was
pulled (fast path) or which tools were called (fallback path) — useful
for building employee trust and for debugging.

## Why tools instead of letting the LLM write SQL (fallback path)

The fallback agent uses named, parameterized tools (`get_account_details`,
`diagnose_account_issue`, etc.) rather than free-form SQL generation:

- A model-written SQL query is reviewed by nobody before it runs.
- Anything an employee pastes in (a customer's email, a ticket) is
  untrusted text the model reads before deciding what to query — a
  prompt-injection surface if the model is also the one writing SQL.
- A fixed set of tools is auditable: security/compliance review can look
  at N functions and know exactly what data any run could touch.

A `run_readonly_sql` tool exists in `tools.py` for local experimentation
only (blocks non-SELECT statements) — it is **not** included in the
agent's default tool set.

## Before this touches anything real

This is a prototype demonstrating the pattern, not a deployable bank
tool. Before connecting it to a real core-banking system, at minimum:

- **Auth & access control**: employee login (SSO/SAML) and per-role
  permissions on which tools/customers an employee can query — none of
  that exists here.
- **Least-privilege DB access**: a read-only account scoped to exactly
  the needed tables, on a replica — not the primary transactional DB.
- **PII handling**: mask/redact full account numbers etc. in responses
  and logs unless the employee's role requires seeing them.
- **Audit logging**: replace the CSV in `agent.py` with a proper,
  tamper-evident audit trail — banks generally have specific regulatory
  requirements a flat file won't satisfy.
- **Rate limiting / monitoring** for unusual query patterns.
- **Compliance sign-off** (data residency, retention, applicable banking
  regulations in your jurisdiction) — general engineering pointer, not
  legal advice.

## Extending it

- New "problem type" to detect fast: add the relevant column(s) to
  `accounts`/`transactions` in `database.py`, and mention the new field
  in `QUICK_ANSWER_PROMPT` (agent.py) so the LLM knows to check it.
- New fallback capability: add a new `@tool` function in `tools.py` and
  append it to `SAFE_TOOLS`. Keep write actions (freezing a card,
  reversing a charge) behind a human-approval step rather than letting
  the agent execute them directly.
