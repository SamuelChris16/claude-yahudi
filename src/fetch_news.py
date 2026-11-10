"""Berita (Alpha Vantage + Finnhub): whitelist, dedup, tier BERITA / RUMOR-TERBATAS."""
import difflib
import hashlib
import os
import time
from datetime import datetime, timedelta

import requests

from db import all_tickers, fetched_today, get_conn, load_config, log_fetch, requests_today


def _av_news(conn, cfg, tickers):
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    b = cfg["api_budget"]["alphavantage"]
    if not key or fetched_today(conn, "alphavantage", "news", "ALL"):
        return []
    if requests_today(conn, "alphavantage") >= b["daily_limit"] - b["reserve"]:
        return []
    since = (datetime.now() - timedelta(days=cfg["news"]["lookback_days"])).strftime("%Y%m%dT0000")
    r = requests.get("https://www.alphavantage.co/query", params={
        "function": "NEWS_SENTIMENT", "tickers": ",".join(tickers), "time_from": since,
        "limit": 200, "sort": "LATEST", "apikey": key}, timeout=30)
    data = r.json()
    if "feed" not in data:
        log_fetch(conn, "alphavantage", "news", "ALL", "limited")
        return []
    log_fetch(conn, "alphavantage", "news", "ALL", "ok")
    items = []
    for it in data["feed"]:
        for ts in it.get("ticker_sentiment", []):
            if ts["ticker"] in tickers:
                pub = datetime.strptime(it["time_published"], "%Y%m%dT%H%M%S").isoformat()
                items.append(dict(ticker=ts["ticker"], source=it.get("source", ""), title=it["title"],
                                  summary=it.get("summary", ""), url=it["url"], published=pub,
                                  sentiment=float(ts.get("ticker_sentiment_score", 0))))
    return items


def _finnhub_news(conn, cfg, tickers):
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return []
    to = datetime.now()
    frm = to - timedelta(days=cfg["news"]["lookback_days"])
    items = []
    for t in tickers:
        if fetched_today(conn, "finnhub", "company-news", t):
            continue
        try:
            r = requests.get("https://finnhub.io/api/v1/company-news", params={
                "symbol": t, "from": frm.strftime("%Y-%m-%d"), "to": to.strftime("%Y-%m-%d"),
                "token": key}, timeout=30)
            r.raise_for_status()
            for it in r.json():
                items.append(dict(ticker=t, source=it.get("source", ""), title=it.get("headline", ""),
                                  summary=it.get("summary", ""), url=it.get("url", ""),
                                  published=datetime.fromtimestamp(it.get("datetime", 0)).isoformat(),
                                  sentiment=None))
            log_fetch(conn, "finnhub", "company-news", t, "ok")
        except Exception:  # noqa: BLE001
            log_fetch(conn, "finnhub", "company-news", t, "error")
        time.sleep(1.1)
    return items


def _process(items, cfg):
    n = cfg["news"]
    wl = [s.lower() for s in n["whitelist_sources"]]
    items = [i for i in items if i["title"] and any(w in i["source"].lower() for w in wl)]
    items.sort(key=lambda i: i["published"])

    clusters = []  # dedup: URL sama atau judul mirip (>0.8) untuk ticker yang sama
    for it in items:
        for c in clusters:
            if c["ticker"] == it["ticker"] and (
                c["url"] == it["url"]
                or difflib.SequenceMatcher(None, c["title"].lower(), it["title"].lower()).ratio() > 0.8
            ):
                c["_sources"].add(it["source"])
                break
        else:
            it["_sources"] = {it["source"]}
            clusters.append(it)

    out = []
    for c in clusters:
        text = f"{c['title']} {c['summary']}".lower()
        c["tier"] = "RUMOR-TERBATAS" if any(p in text for p in n["rumor_patterns"]) else "BERITA"
        c["material"] = int(any(k.lower() in text for k in n["material_keywords"]))
        c["n_outlets"] = len(c["_sources"])
        c["summary"] = c["summary"][:300]
        c["id"] = hashlib.sha1(f"{c['ticker']}{c['url']}".encode()).hexdigest()
        out.append(c)

    # batasi per ticker: material dulu, lalu terbaru
    final = []
    for t in {c["ticker"] for c in out}:
        group = sorted((c for c in out if c["ticker"] == t), key=lambda c: (c["material"], c["published"]), reverse=True)
        final += group[: n["max_items_per_ticker"]]
    return final


def run(conn, cfg):
    tickers = all_tickers(cfg)
    raw = _av_news(conn, cfg, tickers) + _finnhub_news(conn, cfg, tickers)
    final = _process(raw, cfg)
    conn.executemany(
        "INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(c["id"], c["ticker"], c["source"], c["title"], c["summary"], c["url"], c["published"],
          c["sentiment"], c["tier"], c["n_outlets"], c["material"]) for c in final],
    )
    conn.commit()
    return {"raw": len(raw), "kept": len(final),
            "rumor": sum(c["tier"] == "RUMOR-TERBATAS" for c in final),
            "material": sum(c["material"] for c in final)}


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
