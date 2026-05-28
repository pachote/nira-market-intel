"""
nira-market-intel MCP Server
Live market intelligence: Google Trends, SERP, domain traffic, keyword opportunities.
"""
from mcp.server.fastmcp import FastMCP
import os
import re
import json
import time
import requests
from typing import Optional

mcp = FastMCP(
    "nira-market-intel",
    instructions=(
        "Live market intelligence tools for keyword trends, SERP ranking, "
        "domain traffic estimation, and content opportunity discovery. "
        "Uses pytrends for Google Trends data. Falls back gracefully when rate-limited."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_trend_req():
    from pytrends.request import TrendReq
    return TrendReq(hl="en-US", tz=360)


def _safe_trends(fn):
    """Wrap pytrends calls — return rate-limit dict on 429/Too Many Requests."""
    try:
        return fn()
    except Exception as e:
        msg = str(e).lower()
        if "429" in msg or "too many" in msg or "rate" in msg:
            return {"error": "rate_limited", "retry_after": 60}
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def trends_get(
    keywords: list[str],
    timeframe: str = "now 7-d",
    geo: str = "US",
) -> dict:
    """
    Get Google Trends interest-over-time for up to 5 keywords.

    Args:
        keywords: List of keywords (max 5).
        timeframe: pytrends timeframe string, e.g. 'now 7-d', 'now 1-m', 'today 12-m'.
        geo: Two-letter country code or empty string for worldwide.

    Returns:
        {keywords, data: {keyword: [{date, value}]}, trending_now: list}
    """
    keywords = keywords[:5]

    def _run():
        pt = _get_trend_req()
        pt.build_payload(keywords, timeframe=timeframe, geo=geo)
        df = pt.interest_over_time()

        if df is None or df.empty:
            return {
                "keywords": keywords,
                "data": {k: [] for k in keywords},
                "trending_now": [],
            }

        data = {}
        for kw in keywords:
            if kw in df.columns:
                data[kw] = [
                    {"date": str(idx.date()), "value": int(row[kw])}
                    for idx, row in df.iterrows()
                ]
            else:
                data[kw] = []

        # trending_now = keywords with last value > 70
        trending_now = []
        for kw, points in data.items():
            if points and points[-1]["value"] > 70:
                trending_now.append(kw)

        return {"keywords": keywords, "data": data, "trending_now": trending_now}

    result = _safe_trends(_run)
    return result


@mcp.tool()
def trends_rising(keyword: str, geo: str = "US") -> dict:
    """
    Get rising and top related queries for a keyword.

    Args:
        keyword: Seed keyword to analyse.
        geo: Two-letter country code or empty for worldwide.

    Returns:
        {keyword, rising: [{query, value}], top: [{query, value}]}
    """
    def _run():
        pt = _get_trend_req()
        pt.build_payload([keyword], geo=geo)
        related = pt.related_queries()

        rising = []
        top = []
        if related and keyword in related:
            kw_data = related[keyword]
            if kw_data.get("rising") is not None and not kw_data["rising"].empty:
                rising = [
                    {"query": row["query"], "value": str(row["value"])}
                    for _, row in kw_data["rising"].iterrows()
                ]
            if kw_data.get("top") is not None and not kw_data["top"].empty:
                top = [
                    {"query": row["query"], "value": int(row["value"])}
                    for _, row in kw_data["top"].iterrows()
                ]

        return {"keyword": keyword, "rising": rising[:20], "top": top[:20]}

    return _safe_trends(_run)


@mcp.tool()
def serp_check(keyword: str, site: str = None) -> dict:
    """
    Check SERP results for a keyword. Uses Google Custom Search API if env vars
    GOOGLE_CSE_KEY and GOOGLE_CSE_ID are set; otherwise falls back to DuckDuckGo.

    Args:
        keyword: Search query.
        site: Optional domain to find rank for (e.g. 'beatsyncpro.ai').

    Returns:
        {keyword, results: [{title, url, snippet}], site_rank: int or null}
    """
    results = []
    site_rank = None

    cse_key = os.environ.get("GOOGLE_CSE_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")

    if cse_key and cse_id:
        try:
            resp = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": cse_key, "cx": cse_id, "q": keyword, "num": 10},
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            for i, item in enumerate(items):
                entry = {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
                results.append(entry)
                if site and site.lower() in entry["url"].lower() and site_rank is None:
                    site_rank = i + 1
        except Exception as e:
            results = [{"title": "CSE error", "url": "", "snippet": str(e)}]
    else:
        # DuckDuckGo HTML fallback
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
            resp = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": keyword},
                headers=headers,
                timeout=10,
            )
            # Extract result blocks with regex
            titles = re.findall(r'class="result__a"[^>]*>([^<]+)<', resp.text)
            urls = re.findall(r'class="result__url"[^>]*>\s*([^\s<]+)', resp.text)
            snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', resp.text)

            for i in range(min(len(titles), 10)):
                url = urls[i] if i < len(urls) else ""
                entry = {
                    "title": titles[i].strip(),
                    "url": url.strip(),
                    "snippet": snippets[i].strip() if i < len(snippets) else "",
                }
                results.append(entry)
                if site and site.lower() in url.lower() and site_rank is None:
                    site_rank = i + 1
        except Exception as e:
            results = [{"title": "DDG error", "url": "", "snippet": str(e)}]

    return {"keyword": keyword, "results": results, "site_rank": site_rank}


@mcp.tool()
def domain_traffic(domain: str) -> dict:
    """
    Estimate monthly traffic for a domain via SimilarWeb public page scrape.
    Best-effort — returns nulls if blocked.

    Args:
        domain: Domain to analyse, e.g. 'beatsyncpro.ai'.

    Returns:
        {domain, monthly_visits, global_rank, category_rank}
    """
    domain = domain.lstrip("https://").lstrip("http://").rstrip("/")
    monthly_visits = None
    global_rank = None
    category_rank = None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        url = f"https://www.similarweb.com/website/{domain}/"
        resp = requests.get(url, headers=headers, timeout=15)
        text = resp.text

        # Try to parse JSON-LD or visible metrics
        visits_match = re.search(
            r'"totalVisits"\s*:\s*"?([0-9.,KMB]+)"?', text, re.IGNORECASE
        )
        if visits_match:
            monthly_visits = visits_match.group(1)

        rank_match = re.search(
            r'"globalRank"\s*[:{]\s*"?([0-9,]+)"?', text, re.IGNORECASE
        )
        if rank_match:
            global_rank = rank_match.group(1)

        cat_match = re.search(
            r'"categoryRank"\s*[:{]\s*"?([0-9,]+)"?', text, re.IGNORECASE
        )
        if cat_match:
            category_rank = cat_match.group(1)

        # Fallback: look for visible text patterns
        if not monthly_visits:
            vis_match = re.search(
                r'([0-9]+\.?[0-9]*\s*[KMB]?)\s*(?:Total\s*)?Visits', text, re.IGNORECASE
            )
            if vis_match:
                monthly_visits = vis_match.group(1).strip()

    except Exception as e:
        return {
            "domain": domain,
            "monthly_visits": None,
            "global_rank": None,
            "category_rank": None,
            "error": str(e),
        }

    return {
        "domain": domain,
        "monthly_visits": monthly_visits,
        "global_rank": global_rank,
        "category_rank": category_rank,
    }


@mcp.tool()
def keyword_opportunities(niche: str, seed_keywords: list[str]) -> dict:
    """
    Analyse a niche for content opportunities. Runs trends on all seeds,
    surfaces hot keywords (value > 50), gets rising queries for top 3.

    Args:
        niche: Niche label, e.g. 'music production'.
        seed_keywords: List of seed keywords to analyse.

    Returns:
        {niche, hot_keywords, rising_queries, recommended_content}
    """
    hot_keywords = []
    rising_queries = []
    recommended_content = []

    # Batch seeds into groups of 5 (pytrends max)
    batches = [seed_keywords[i:i+5] for i in range(0, len(seed_keywords), 5)]
    all_data = {}

    for batch in batches:
        result = trends_get(batch, timeframe="now 1-m")
        if "error" in result:
            break
        for kw, points in result.get("data", {}).items():
            all_data[kw] = points

    # Find hot keywords (avg value > 50 over last 7 points)
    for kw, points in all_data.items():
        if not points:
            continue
        recent = points[-7:] if len(points) >= 7 else points
        avg = sum(p["value"] for p in recent) / len(recent)
        if avg > 50:
            hot_keywords.append({"keyword": kw, "avg_interest": round(avg, 1)})

    hot_keywords.sort(key=lambda x: x["avg_interest"], reverse=True)

    # Get rising queries for top 3 hot keywords
    for item in hot_keywords[:3]:
        kw = item["keyword"]
        rising_result = trends_rising(kw)
        if "error" not in rising_result:
            for r in rising_result.get("rising", [])[:5]:
                rising_queries.append({"source_keyword": kw, **r})
        time.sleep(1)  # Be gentle with pytrends

    # Recommended content = hot kws + top rising queries combined
    seen = set()
    for item in hot_keywords[:5]:
        kw = item["keyword"]
        if kw not in seen:
            recommended_content.append(f"In-depth guide: {kw}")
            seen.add(kw)
    for r in rising_queries[:5]:
        q = r["query"]
        if q not in seen:
            recommended_content.append(f"Trending topic: {q}")
            seen.add(q)

    return {
        "niche": niche,
        "hot_keywords": hot_keywords,
        "rising_queries": rising_queries,
        "recommended_content": recommended_content,
    }


@mcp.tool()
def market_summary(topics: list[str] = None) -> dict:
    """
    Get a trends snapshot for music production keywords (or custom topics).

    Args:
        topics: Optional list of topics. Defaults to BeatSync PRO core keywords.

    Returns:
        Trends snapshot with interest data and trending keywords.
    """
    if not topics:
        topics = [
            "type beat",
            "free beats",
            "drill instrumental",
            "lo-fi hip hop",
            "trap beat 2026",
        ]

    # Batch into 5s
    batches = [topics[i:i+5] for i in range(0, len(topics), 5)]
    combined_data = {}
    all_trending = []

    for batch in batches:
        result = trends_get(batch, timeframe="now 7-d")
        if "error" in result:
            return result
        combined_data.update(result.get("data", {}))
        all_trending.extend(result.get("trending_now", []))
        if len(batches) > 1:
            time.sleep(2)

    # Compute summary stats per keyword
    summary = {}
    for kw, points in combined_data.items():
        if not points:
            summary[kw] = {"avg": 0, "peak": 0, "latest": 0, "trend": "flat"}
            continue
        values = [p["value"] for p in points]
        avg = sum(values) / len(values)
        peak = max(values)
        latest = values[-1]
        # Simple trend: compare last 3 vs first 3
        if len(values) >= 6:
            early_avg = sum(values[:3]) / 3
            late_avg = sum(values[-3:]) / 3
            if late_avg > early_avg * 1.2:
                trend = "rising"
            elif late_avg < early_avg * 0.8:
                trend = "falling"
            else:
                trend = "stable"
        else:
            trend = "stable"
        summary[kw] = {
            "avg": round(avg, 1),
            "peak": peak,
            "latest": latest,
            "trend": trend,
        }

    return {
        "topics": topics,
        "summary": summary,
        "trending_now": list(set(all_trending)),
        "snapshot_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


if __name__ == "__main__":
    mcp.run()
