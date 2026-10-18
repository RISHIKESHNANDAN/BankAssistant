import streamlit as st
from database import init_db
from agent import build_agent, ask_agent, quick_answer, get_llm

st.set_page_config(page_title="Bank Employee Assist (Prototype)", layout="wide")

init_db()  # creates the demo DB on first run

st.title("🏦 Bank Employee Assist — Prototype")
st.caption(
    "Type a question about a customer, account, transaction, or complaint. "
    "Demo data only — not connected to any real banking system."
)

with st.sidebar:
    st.markdown("### Session")
    employee_id = st.text_input("Employee ID", value="EMP-001")
    st.markdown("---")
    st.markdown("### Try asking")
    st.markdown(
        "**\"Not working\" questions:**\n"
        "- *My money isn't transferring, ACC-5006*\n"
        "- *Customer says ACC-5005 isn't working*\n"
        "- *Why did the transfer fail on ACC-5003?*\n"
        "- *Card isn't working, ACC-5007*\n"
        "- *Why is ACC-5004 frozen?*\n\n"
        "**Lookups:**\n"
        "- *Balance on ACC-5001?*\n"
        "- *Find customer Manoj Kundu*\n"
        "- *Complaints for CUST-1003?*\n"
        "- *duplicate transaction charge*"
    )
    st.markdown("---")
    st.caption(
        "This is a prototype: demo SQLite DB, no login/auth, no real "
        "customer data. See README before pointing this at anything real."
    )

if "llm" not in st.session_state:
    st.session_state.llm = get_llm()
if "agent" not in st.session_state:
    st.session_state.agent = build_agent(allow_raw_sql=False)
if "history" not in st.session_state:
    st.session_state.history = []


def render_context(context: dict):
    """Show the raw rows found, as a quick-scan table set, instantly."""
    labels = {
        "customers": "Customer(s)", "accounts": "Account(s)",
        "transactions": "Transaction(s)", "complaints": "Complaint(s)",
    }
    any_rows = False
    for key, label in labels.items():
        rows = context.get(key) or []
        if rows:
            any_rows = True
            st.markdown(f"**{label}**")
            st.dataframe(rows, hide_index=True, use_container_width=True)
    return any_rows


for turn in st.session_state.history:
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])
        if turn.get("context"):
            with st.expander("Data used", expanded=False):
                render_context(turn["context"])
        if turn.get("trace"):
            with st.expander("Agent trace (multi-step fallback)"):
                for step in turn["trace"]:
                    if step["type"] == "tool_call":
                        st.markdown(f"**Called `{step['tool']}`** with `{step['args']}`")
                    else:
                        st.code(str(step["content"]), language="json")

question = st.chat_input("Search a customer, account, transaction, or complaint...")

if question:
    st.session_state.history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        # Fast path first: local DB lookup + a single LLM call.
        with st.spinner("Searching..."):
            result = quick_answer(st.session_state.llm, employee_id or "unknown", question)

        if not result["used_fallback"]:
            st.markdown(result["answer"])
            with st.expander("Data used", expanded=False):
                render_context(result["context"])
            st.session_state.history.append({
                "role": "assistant", "content": result["answer"], "context": result["context"],
            })
        else:
            # Nothing matched on a direct lookup — fall back to the
            # slower, multi-step tool-calling agent for a broader search.
            st.caption("No direct match — running a broader search...")
            with st.spinner("Digging deeper..."):
                deep = ask_agent(st.session_state.agent, employee_id or "unknown", question)
            st.markdown(deep["answer"])
            with st.expander("Agent trace (multi-step fallback)"):
                for step in deep["trace"]:
                    if step["type"] == "tool_call":
                        st.markdown(f"**Called `{step['tool']}`** with `{step['args']}`")
                    else:
                        st.code(str(step["content"]), language="json")
            st.session_state.history.append({
                "role": "assistant", "content": deep["answer"], "trace": deep["trace"],
            })
