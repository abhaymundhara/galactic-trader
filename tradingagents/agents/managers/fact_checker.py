from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict

import requests


def _extract_urls(text: str) -> List[str]:
    if not text:
        return []
    urls = re.findall(
        r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
        text,
    )
    cleaned = [u.rstrip(").,;]") for u in urls]
    return list(dict.fromkeys(cleaned))


def _verify_url(url: str) -> Dict[str, str]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
            stream=True,
        )
        code = resp.status_code
        if 200 <= code < 400:
            status = "VALID"
        elif code == 403:
            status = "VALID (403 protected)"
        elif code == 404:
            status = "NOT FOUND (404)"
        else:
            status = f"STATUS {code}"
    except Exception as e:
        status = f"ERROR ({e})"
    return {"url": url, "status": status}


def _check_urls(text: str) -> List[Dict[str, str]]:
    urls = _extract_urls(text)
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=5) as pool:
        return list(pool.map(_verify_url, urls))


def create_fact_checker(llm):
    def fact_checker_node(state) -> dict:
        invest_state = state["investment_debate_state"]
        current_response = invest_state.get("current_response", "")
        news_report = state.get("news_report", "")

        checked = _check_urls(news_report + "\n" + current_response)
        url_report = "\n".join(f"- {x['url']}: {x['status']}" for x in checked) or "- no urls found"

        prompt = f"""You are a strict fact checker for an investment debate.
Review the latest debate response and flag concrete factual inconsistencies.
Be strict with numbers and event claims.

Latest response:
{current_response}

Source reports:
Market: {state.get('market_report', '')}
Sentiment: {state.get('sentiment_report', '')}
News: {news_report}
Fundamentals: {state.get('fundamentals_report', '')}

URL verification:
{url_report}

Output:
- If issues exist: start with 'CORRECTION NEEDED:' and list them.
- If no issues found: start with 'VERIFIED:' and provide a concise confirmation.
"""
        result = llm.invoke(prompt).content

        new_state = dict(invest_state)
        new_state["verified_urls"] = checked

        if "CORRECTION NEEDED" in result:
            updated = f"{current_response}\n\n[FACT CHECK WARNING]\n{result}"
            new_state["current_response"] = updated
            new_state["history"] = (new_state.get("history", "") + f"\n\n[Fact Checker]\n{result}").strip()
        else:
            new_state["history"] = (new_state.get("history", "") + f"\n\n[Fact Checker]\n{result}").strip()

        return {"investment_debate_state": new_state}

    return fact_checker_node
