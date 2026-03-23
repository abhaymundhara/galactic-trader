from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

from scheduler_service import AnalysisScheduler, local_time_to_utc
from tradingagents.agents.utils.memory import FinancialSituationMemory
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

load_dotenv()

st.set_page_config(page_title="TradingAgents Dashboard", layout="wide")
st.title("TradingAgents Dashboard")
st.caption("Manual analysis + scheduled jobs + persistent markdown memory")


@st.cache_resource
def get_scheduler():
    return AnalysisScheduler()


scheduler = get_scheduler()


def convert_korean_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.endswith(".KS") or s.endswith(".KQ"):
        return f"KRX:{s.split('.')[0]}"
    return s


def parse_symbol_from_choice(choice: str) -> str:
    symbol = choice.split("|")[0].strip()
    return convert_korean_symbol(symbol)


def search_ticker_suggestions(query: str) -> list[str]:
    if not query or len(query) < 2:
        return []
    try:
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        res = requests.get(
            url,
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        res.raise_for_status()
        data = res.json()
        out = []
        for item in data.get("quotes", [])[:8]:
            symbol = item.get("symbol")
            if not symbol:
                continue
            name = item.get("shortname", "")
            exch = item.get("exchange", "")
            out.append(f"{symbol} | {name} ({exch})")
        return out
    except Exception:
        return []


def summarize_reason(text: str, limit: int = 280) -> str:
    compact = re.sub(r"\s+", " ", (text or "")).strip()
    if not compact:
        return "No reasoning returned"
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


with st.sidebar:
    st.header("Settings")
    ticker_input = st.text_input("Ticker", value="XAUUSD")

    suggestions = search_ticker_suggestions(ticker_input)
    if suggestions:
        selected = st.selectbox("Suggestions", options=["(keep input)"] + suggestions)
        if selected != "(keep input)":
            ticker_input = parse_symbol_from_choice(selected)

    ticker = convert_korean_symbol(ticker_input)
    target_date = st.date_input("Analysis Date", value=datetime.now())

    llm_provider = st.selectbox(
        "LLM Provider",
        options=["ollama", "openai", "google", "anthropic", "xai", "openrouter"],
        index=0,
    )
    deep_model = st.text_input("Deep model", value="qwen3:latest")
    quick_model = st.text_input("Quick model", value="qwen3:latest")
    debate_rounds = st.slider("Debate rounds", min_value=1, max_value=5, value=2)
    risk_rounds = st.slider("Risk rounds", min_value=1, max_value=5, value=2)

    st.divider()
    st.subheader("Persistent Memory")
    markdown_path = st.text_input(
        "Markdown folder (Obsidian or any .md directory)",
        value="",
    )
    load_memory = st.button("Load markdown into memory", use_container_width=True)

    st.divider()
    st.subheader("Scheduler")
    tz_name = st.selectbox(
        "Timezone",
        options=[
            "UTC",
            "Europe/London",
            "America/New_York",
            "Asia/Seoul",
            "Asia/Tokyo",
            "Asia/Hong_Kong",
        ],
        index=1,
    )
    frequency = st.selectbox("Frequency", options=["Every day", "Weekdays"], index=1)
    run_hour = st.number_input("Local hour", min_value=0, max_value=23, value=9)
    run_minute = st.number_input("Local minute", min_value=0, max_value=59, value=0)
    schedule_btn = st.button("Add schedule", use_container_width=True)

run_btn = st.button("Run analysis now", type="primary")

if load_memory and markdown_path:
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = llm_provider
    cfg["deep_think_llm"] = deep_model
    cfg["quick_think_llm"] = quick_model
    mem = FinancialSituationMemory("invest_judge_memory", cfg)
    message = mem.load_from_obsidian(markdown_path)
    st.info(message)

if schedule_btn:
    utc_hour, utc_minute = local_time_to_utc(int(run_hour), int(run_minute), tz_name)
    day_of_week = "mon-fri" if frequency == "Weekdays" else "*"
    cron_expr = f"{utc_minute} {utc_hour} * * {day_of_week}"
    ok, msg = scheduler.add_job(
        ticker=ticker,
        cron_expr=cron_expr,
        llm_provider=llm_provider,
        deep_model=deep_model,
        quick_model=quick_model,
        debate_rounds=debate_rounds,
        risk_rounds=risk_rounds,
    )
    if ok:
        st.success(msg)
    else:
        st.error(msg)

if run_btn:
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = llm_provider
    cfg["deep_think_llm"] = deep_model
    cfg["quick_think_llm"] = quick_model
    cfg["max_debate_rounds"] = debate_rounds
    cfg["max_risk_discuss_rounds"] = risk_rounds

    with st.spinner(f"Running analysis for {ticker}..."):
        ta = TradingAgentsGraph(debug=False, config=cfg)
        final_state, rating = ta.propagate(ticker, target_date.strftime("%Y-%m-%d"))

    st.subheader("Decision")
    st.write(f"**Ticker:** `{ticker}`")
    st.write(f"**Rating:** `{rating}`")
    st.write(summarize_reason(final_state.get("final_trade_decision", "")))

    st.subheader("Reports")
    st.markdown("### Market")
    st.write(final_state.get("market_report", ""))
    st.markdown("### News")
    st.write(final_state.get("news_report", ""))
    st.markdown("### Sentiment")
    st.write(final_state.get("sentiment_report", ""))
    st.markdown("### Fundamentals")
    st.write(final_state.get("fundamentals_report", ""))

st.divider()
st.subheader("Scheduled Jobs")
jobs = scheduler.list_jobs(timezone_name=tz_name)
if not jobs:
    st.caption("No scheduled jobs")
else:
    for job in jobs:
        cols = st.columns([4, 2, 2, 1])
        cols[0].write(f"**{job['ticker']}**")
        cols[1].write(job["schedule_utc"])
        cols[2].write(job["next_run_local"])
        if cols[3].button("Remove", key=f"rm_{job['id']}"):
            ok, msg = scheduler.remove_job(job["id"])
            if ok:
                st.success(msg)
                st.rerun()
            st.error(msg)

st.divider()
st.subheader("Recent Scheduler Outputs")
log_root = Path("logs")
if log_root.exists():
    status_files = sorted(log_root.glob("**/*_status.json"), reverse=True)[:10]
    for sf in status_files:
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
            st.write(
                f"`{data.get('status', 'unknown')}` {data.get('ticker', '')} | "
                f"start: {data.get('start_time', '')} | "
                f"summary: {data.get('summary_file', '-')}"
            )
        except Exception:
            continue
else:
    st.caption("No logs yet")
