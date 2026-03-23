from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from croniter import croniter

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_time_to_utc(local_hour: int, local_minute: int, timezone_name: str) -> tuple[int, int]:
    """Convert local clock time to UTC clock time for cron scheduling."""
    user_tz = pytz.timezone(timezone_name)
    today_local = datetime.now(user_tz).date()
    local_dt = user_tz.localize(datetime(today_local.year, today_local.month, today_local.day, local_hour, local_minute))
    utc_dt = local_dt.astimezone(pytz.UTC)
    return utc_dt.hour, utc_dt.minute


class AnalysisScheduler:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        db_url = "sqlite:///jobs.sqlite"
        jobstores = {"default": SQLAlchemyJobStore(url=db_url)}
        executors = {"default": ThreadPoolExecutor(10)}
        job_defaults = {"coalesce": False, "max_instances": 2}
        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone=pytz.UTC,
        )
        self.scheduler.start()

    def add_job(
        self,
        ticker: str,
        cron_expr: str,
        llm_provider: str = "ollama",
        deep_model: str = "qwen3:latest",
        quick_model: str = "qwen3:latest",
        debate_rounds: int = 2,
        risk_rounds: int = 2,
    ) -> tuple[bool, str]:
        if not croniter.is_valid(cron_expr):
            return False, f"Invalid cron expression: {cron_expr}"

        job_id = f"{ticker}_{cron_expr.replace(' ', '_')}"
        if self.scheduler.get_job(job_id):
            return False, "Job already exists"

        parts = cron_expr.split()
        if len(parts) != 5:
            return False, "Cron expression must have 5 fields"
        minute, hour, day, month, day_of_week = parts

        self.scheduler.add_job(
            func=run_analysis_task,
            trigger="cron",
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            id=job_id,
            name=f"Analyze {ticker}",
            replace_existing=True,
            args=[ticker, llm_provider, deep_model, quick_model, debate_rounds, risk_rounds],
        )
        return True, f"Job added: {ticker} @ {cron_expr}"

    def remove_job(self, job_id: str) -> tuple[bool, str]:
        try:
            self.scheduler.remove_job(job_id)
            return True, "Job removed"
        except Exception as e:
            return False, str(e)

    def list_jobs(self, timezone_name: str = "UTC") -> List[Dict[str, str]]:
        user_tz = pytz.timezone(timezone_name)
        jobs = []
        for job in self.scheduler.get_jobs():
            parts = job.id.split("_")
            ticker = parts[0]
            schedule = " ".join(parts[1:]) if len(parts) >= 6 else "Custom"

            next_run_local = "N/A"
            if job.next_run_time:
                next_run_local = job.next_run_time.astimezone(user_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

            jobs.append(
                {
                    "id": job.id,
                    "ticker": ticker,
                    "schedule_utc": schedule,
                    "next_run_local": next_run_local,
                }
            )
        return jobs


def run_analysis_task(
    ticker: str,
    llm_provider: str,
    deep_model: str,
    quick_model: str,
    debate_rounds: int,
    risk_rounds: int,
):
    started = _now_utc()
    date_str = started.strftime("%Y-%m-%d")
    time_str = started.strftime("%H%M%S")

    log_dir = Path("logs") / date_str / ticker
    log_dir.mkdir(parents=True, exist_ok=True)
    status_file = log_dir / f"{time_str}_status.json"
    summary_file = log_dir / f"{time_str}_summary.json"

    def write_status(payload: Dict):
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    write_status(
        {
            "status": "running",
            "ticker": ticker,
            "start_time": started.isoformat(),
            "stage": "initializing",
            "progress": 10,
        }
    )

    try:
        cfg = DEFAULT_CONFIG.copy()
        cfg["llm_provider"] = llm_provider
        cfg["deep_think_llm"] = deep_model
        cfg["quick_think_llm"] = quick_model
        cfg["max_debate_rounds"] = debate_rounds
        cfg["max_risk_discuss_rounds"] = risk_rounds

        write_status(
            {
                "status": "running",
                "ticker": ticker,
                "start_time": started.isoformat(),
                "stage": "analysis",
                "progress": 60,
            }
        )

        ta = TradingAgentsGraph(debug=False, config=cfg)
        final_state, rating = ta.propagate(ticker, date_str)

        finished = _now_utc()
        duration = (finished - started).total_seconds()
        payload = {
            "ticker": ticker,
            "date": date_str,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": duration,
            "rating": rating,
            "final_trade_decision": final_state.get("final_trade_decision", ""),
            "trader_plan": final_state.get("trader_investment_plan", ""),
            "market_report": final_state.get("market_report", ""),
            "news_report": final_state.get("news_report", ""),
            "sentiment_report": final_state.get("sentiment_report", ""),
            "fundamentals_report": final_state.get("fundamentals_report", ""),
        }
        with open(summary_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        write_status(
            {
                "status": "completed",
                "ticker": ticker,
                "start_time": started.isoformat(),
                "end_time": finished.isoformat(),
                "duration_seconds": duration,
                "progress": 100,
                "summary_file": str(summary_file),
                "rating": rating,
            }
        )
    except Exception as e:
        finished = _now_utc()
        write_status(
            {
                "status": "failed",
                "ticker": ticker,
                "start_time": started.isoformat(),
                "end_time": finished.isoformat(),
                "error": str(e),
            }
        )
