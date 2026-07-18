"""Custom function tools for trade_agent.

Both functions below are plain Python callables with type hints and
docstrings — ADK auto-wraps these as FunctionTool when placed directly in an
Agent's `tools=[...]` list, no manual wrapping needed.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

LEDGER_PATH = Path(__file__).parent / "ledger" / "dry_run_trades.jsonl"


def get_news_sentiment(query: str) -> str:
    """Fetches recent news headlines for a stock/company/index and returns them for sentiment analysis.

    Args:
        query: The company name, ticker, or topic to search news for (e.g.
            "Reliance Industries", "NIFTY 50", "RBI monetary policy").

    Returns:
        A formatted string of recent headlines with source, publish date, and
        URL, newest first — for the agent to read and summarize sentiment
        from. Returns an explanatory message instead if the news API key is
        not configured or the request fails, so the caller can proceed
        without a hard failure.
    """
    api_key = os.getenv("NEWS_API_KEY")
    if not api_key:
        return (
            "NEWS_API_KEY is not set — no news data available for "
            f"'{query}'. Proceed using technical signals only and note that "
            "news/sentiment input was unavailable this cycle."
        )

    base_url = os.getenv("NEWS_API_BASE_URL", "https://newsapi.org/v2")
    try:
        response = requests.get(
            f"{base_url}/everything",
            params={
                "q": query,
                "apiKey": api_key,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": 8,
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return f"News API request failed for '{query}': {exc}. Proceed using technical signals only."

    articles = payload.get("articles", [])
    if not articles:
        return f"No recent news found for '{query}'."

    lines = [f"Recent news for '{query}':"]
    for article in articles:
        title = article.get("title", "(no title)")
        source = (article.get("source") or {}).get("name", "unknown source")
        published_at = article.get("publishedAt", "unknown date")
        url = article.get("url", "")
        lines.append(f"- [{published_at}] ({source}) {title} — {url}")
    return "\n".join(lines)


def log_dry_run_trade(order_summary: str, rationale: str) -> str:
    """Appends a simulated (dry-run) order to the local audit ledger instead of placing a real order.

    Args:
        order_summary: A concise description of the order that would have
            been placed (instrument, exchange, transaction type, quantity,
            product, order type, price, stop-loss, target).
        rationale: The reasoning behind the trade decision.

    Returns:
        A confirmation string including the ledger file path.
    """
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "order_summary": order_summary,
        "rationale": rationale,
    }
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return f"Dry-run trade logged to {LEDGER_PATH}."
