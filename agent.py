"""
agent.py
--------
The agent itself. Uses LangGraph's prebuilt ReAct-style agent: the LLM
reads the employee's question, decides which tool(s) to call (search a
customer, pull an account, pull recent transactions, pull complaints...),
looks at the results, and either calls another tool or answers.

This mirrors the pattern in the warranty-claim project (LLM + structured
steps + a trace you can inspect) but swaps the fixed pipeline for
tool-calling, since "check whatever the employee is asking about" is
open-ended in a way a fixed 4-step pipeline isn't.
"""

import os
import csv
import time
from datetime import datetime
from typing import List, Dict, Any

from dotenv import load_dotenv
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage

from tools import SAFE_TOOLS, ALL_TOOLS_INCLUDING_RAW_SQL
from search_engine import gather_context

load_dotenv()

AUDIT_LOG_PATH = os.path.join(os.path.dirname(__file__), "audit_log.csv")

QUICK_ANSWER_PROMPT = """You are an internal assistant for bank branch employees.
Below is data already retrieved from the organization's database that is
relevant to the employee's question. Answer using ONLY this data - do not
invent account numbers, balances, dates, or ticket statuses.

Be direct and fast to read: lead with the answer in the first sentence,
then the 1-3 supporting facts that back it up. No preamble, no "Based on
the data provided". If the data doesn't actually answer the question,
say plainly what's missing.

For "not working" / "money isn't transferring" / "transaction failed"
style questions, actively diagnose using the fields provided - don't just
dump rows. Check, in this order: account status (Active/Frozen/Blocked/
Dormant) and its status_reason; card_status; whether daily_transfer_limit
has been reached (used_today vs daily_transfer_limit); any transaction
with status Failed or Pending and its failure_reason; and any open
complaints. If several accounts are in the data, focus on the one the
employee named; if none was named, say which account the issue is on.
Lead with the likely cause, then the specifics.

DATA:
{context}

QUESTION: {question}
"""

SYSTEM_PROMPT = """You are an internal assistant for bank branch employees.
You help staff quickly look up customer, account, transaction, and
complaint information to resolve customer issues - fast, so they can
move on to the next customer.

Rules:
- Only use the tools provided to look up information. Never invent
  account numbers, balances, or transaction data.
- If a question requires a customer_id or account_id you don't have yet,
  use search_customer first (by name, phone, or email), then proceed.
- For "not working" style questions - "my account isn't working",
  "my money isn't transferring", "my transaction failed", "why can't I
  withdraw/pay" - call diagnose_account_issue on the account. It checks
  account status, card status, KYC status, daily transfer limit, recent
  failed transactions, and open complaints in one shot. Don't make the
  employee ask five separate questions for this - one diagnose call plus
  your summary should be enough.
- If a lookup returns no results or an error, say so plainly - do not
  guess.
- Keep answers concise and factual, the way you'd talk to a colleague:
  lead with the likely cause / the answer, then the supporting detail
  (balances, dates, exact failure reason, ticket status, etc.). Don't
  dump raw tool output - summarize it.
- Do not reveal information beyond what the employee asked about, and
  never fabricate policy or compliance advice - if asked something
  outside account data (e.g. a legal question), say it's outside what
  you can look up here.
"""


def get_llm():
    from pydantic import SecretStr

    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    def _secret(env_var: str) -> SecretStr | None:
        val = os.getenv(env_var)
        return SecretStr(val) if val else None

    if provider == "groq":
        from langchain_groq import ChatGroq

        model = os.getenv(
            "GROQ_MODEL",
            "openai/gpt-oss-120b"
        )

        return ChatGroq(
            api_key=_secret("GROQ_API_KEY"),
            model=model,
            temperature=0,
        )

    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=_secret("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv(
                "AZURE_OPENAI_API_VERSION",
                "2024-02-01"
            ),
            azure_deployment=os.getenv(
                "AZURE_OPENAI_DEPLOYMENT",
                "gpt-4o"
            ),
            temperature=0,
        )

    else:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            api_key=_secret("OPENAI_API_KEY"),
            model=os.getenv(
                "OPENAI_MODEL",
                "gpt-4o-mini"
            ),
            temperature=0,
        )


def build_agent(allow_raw_sql: bool = False):
    """Build the ReAct agent graph. allow_raw_sql=True enables the
    higher-risk free-form SQL tool - leave False unless you're
    deliberately prototyping locally with it."""
    llm = get_llm()
    tools = ALL_TOOLS_INCLUDING_RAW_SQL if allow_raw_sql else SAFE_TOOLS
    return create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)


def _log_audit_row(employee_id: str, question: str, answer: str, tool_calls: List[str]) -> None:
    """Append a row to a local CSV audit trail. In production this should
    go to a proper, tamper-evident audit log / SIEM, not a flat file -
    banks generally have hard requirements here (who asked what, when,
    about which customer). This is a placeholder to show where it plugs
    in."""
    is_new = not os.path.exists(AUDIT_LOG_PATH)
    with open(AUDIT_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "employee_id", "question", "tools_used", "answer"])
        writer.writerow([
            datetime.utcnow().isoformat(),
            employee_id,
            question,
            "|".join(tool_calls),
            answer,
        ])


def ask_agent(agent, employee_id: str, question: str) -> Dict[str, Any]:
    """Run one turn of the agent and return the answer plus a trace of
    tool calls, for display/debugging in the UI."""
    result = agent.invoke({"messages": [HumanMessage(content=question)]})
    messages = result["messages"]

    trace = []
    tool_names_used = []
    for m in messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                trace.append({"type": "tool_call", "tool": tc["name"], "args": tc["args"]})
                tool_names_used.append(tc["name"])
        elif isinstance(m, ToolMessage):
            trace.append({"type": "tool_result", "tool": m.name, "content": m.content})

    final_answer = messages[-1].content if messages else ""

    _log_audit_row(employee_id, question, final_answer, tool_names_used)

    return {"answer": final_answer, "trace": trace}


def quick_answer(llm, employee_id: str, question: str) -> Dict[str, Any]:
    """The fast path: one local DB lookup (no LLM involved) + exactly ONE
    LLM call to phrase the answer. No multi-step tool-calling loop, so
    this is typically a fraction of the latency of `ask_agent`.

    Returns a dict with the answer, the raw context bundle that was found
    (so the UI can show it instantly, even before/without an LLM call),
    and a `used_fallback` flag - True means the fast path found nothing
    and the caller should route to the slower `ask_agent` instead.
    """
    context = gather_context(question)
    found_anything = context.pop("_found_anything", False)

    if not found_anything:
        return {"answer": None, "context": context, "used_fallback": True}

    import json
    prompt = QUICK_ANSWER_PROMPT.format(
        context=json.dumps(context, indent=2, default=str),
        question=question,
    )
    response = llm.invoke([SystemMessage(content=prompt)])
    answer = response.content.strip()

    _log_audit_row(employee_id, question, answer, ["quick_search"])

    return {"answer": answer, "context": context, "used_fallback": False}
