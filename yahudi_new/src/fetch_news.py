"""Berita (Finnhub + Alpha Vantage): whitelist media, filter relevansi, dedup, tier, dan tanda material.

Perubahan penting:
- Alpha Vantage NEWS_SENTIMENT dipanggil satu ticker per request (beberapa ticker sekaligus berarti
  artikel harus menyebut semuanya). Dibatasi news.alphavantage_max_requests_per_day dan digilir.
- Berita harus menyebut emitennya (nama atau ticker) supaya artikel umum tidak ikut masuk.
- Kata kunci material dicocokkan sebagai kata utuh pada JUDUL saja.
- Waktu terbit disimpan dalam UTC.
"""
import difflib
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import http_client
from db import ROOT, all_tickers, budget_left, fetched_today, get_conn, last_ok, load_config, log_fetch

# nama lain media di whitelist (huruf kecil). Alias pendek hanya cocok bila persis sama.
ALIASES = {
    "wall street journal": ["the wall street journal", "wsj", "wsj.com"],
    "associated press": ["ap news", "apnews", "ap"],
    "financial times": ["ft", "ft.com"],
    "barron's": ["barrons", "barron’s", "barrons.com"],
    "marketwatch": ["market watch"],
    "bloomberg": ["bloomberg news", "bloomberg.com"],
    "reuters": ["reuters.com", "thomson reuters"],
    "cnbc": ["cnbc.com"],
}
_SUFFIX = re.compile(r"(?i)[\s,]*(/[a-z]{2,3}/?|\b(inc|incorporated|corp|corporation|co|company|plc|ltd|limited|llc|lp|"
                     r"holdings?|group|sa|nv|ag|the)\b\.?)\s*$")


def source_allowed(source, whitelist):
    s = re.sub(r"\s+", " ", str(source or "").strip().lower())
    if not s:
        return False
    for w in whitelist:
        w = str(w).strip().lower()
        for name in [w] + ALIASES.get(w, []):
            if s == name or (len(name) > 3 and re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", s)):
                return True
    return False


def clean_company_name(title):
    s = str(title or "").strip()
    for _ in range(4):
        s2 = _SUFFIX.sub("", s).strip(" ,.&")
        if s2 == s:
            break
        s = s2
    return s.lower()


def company_terms(cfg):
    """{ticker: [istilah huruf kecil]} dari config (kunci `names`) dan daftar emiten SEC bila tersedia."""
    out = {}
    sec = {}
    f = ROOT / "data" / "company_tickers.json"
    if f.exists():
        try:
            sec = {v["ticker"]: v["title"] for v in json.loads(f.read_text(encoding="utf-8")).values()}
        except Exception:  # noqa: BLE001
            sec = {}
    for e in cfg["portfolio"]["core"] + cfg["portfolio"]["satellite"]:
        t = e["ticker"]
        terms = [str(x).lower() for x in (e.get("names") or [])]
        name = clean_company_name(sec.get(t) or sec.get(t.replace(".", "-")) or "")
        if name:
            terms.append(name)
            first = name.split()[0]
            if len(name.split()) > 1 and len(first) >= 6:
                terms.append(first)
        out[t] = sorted(set(x for x in terms if len(x) >= 3))
    return out


def is_relevant(ticker, text, terms):
    """True bila teks menyebut ticker (huruf besar, kata utuh) atau salah satu nama emiten.
    Bila nama emiten tidak diketahui sama sekali, berita tidak disaring (None = tidak bisa dinilai)."""
    if re.search(r"(?<![A-Za-z0-9])" + re.escape(ticker) + r"(?![A-Za-z0-9])", text):
        return True
    names = terms.get(ticker) or []
    if not names:
        return None
    low = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", low) for n in names)


def kw_match(patterns, text):
    low = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(str(p).lower()) + r"(?![a-z0-9])", low) for p in patterns)


def _utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _av_news(conn, cfg, tickers):
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    n = cfg["news"]
    cap = int(n.get("alphavantage_max_requests_per_day", 2))
    if not key or cap <= 0:
        return [], []
    min_rel = float(n.get("min_relevance", 0.25))
    since = (datetime.now(timezone.utc) - timedelta(days=n["lookback_days"])).strftime("%Y%m%dT0000")
    used = conn.execute("SELECT COUNT(*) FROM fetch_log WHERE source='alphavantage' AND endpoint='news' "
                        "AND date(ts)=date('now','localtime')").fetchone()[0]
    order = sorted(tickers, key=lambda t: last_ok(conn, "alphavantage", "news", t) or "")   # yang paling lama dulu
    items, errors = [], []
    for t in order:
        if used >= cap or budget_left(conn, cfg) <= 0:
            break
        if fetched_today(conn, "alphavantage", "news", t):
            continue
        used += 1
        try:
            data = http_client.get("https://www.alphavantage.co/query", params={
                "function": "NEWS_SENTIMENT", "tickers": t, "time_from": since, "limit": 50,
                "sort": "LATEST", "apikey": key}).json()
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "alphavantage", "news", t, "error")
            errors.append("%s: %s" % (t, http_client.redact(e)))
            continue
        if "feed" not in data:
            log_fetch(conn, "alphavantage", "news", t, "limited")
            if "Note" in data or "Information" in data:
                break
            continue
        log_fetch(conn, "alphavantage", "news", t, "ok")
        for it in data["feed"]:
            for ts in it.get("ticker_sentiment", []):
                try:
                    if ts["ticker"] != t or float(ts.get("relevance_score", 0)) < min_rel:
                        continue
                    pub = datetime.strptime(it["time_published"], "%Y%m%dT%H%M%S").isoformat(timespec="seconds")
                    items.append(dict(ticker=t, source=it.get("source", ""), title=it["title"], summary=it.get("summary", ""),
                                      url=it["url"], published=pub, sentiment=float(ts.get("ticker_sentiment_score", 0)),
                                      relevance=float(ts.get("relevance_score", 0))))
                except (KeyError, TypeError, ValueError):
                    continue
        time.sleep(cfg["api_budget"]["alphavantage"]["delay_seconds"])
    return items, errors


def _finnhub_news(conn, cfg, tickers):
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return [], []
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=cfg["news"]["lookback_days"])
    items, errors = [], []
    for t in tickers:
        if fetched_today(conn, "finnhub", "company-news", t):
            continue
        try:
            data = http_client.get("https://finnhub.io/api/v1/company-news", params={
                "symbol": t, "from": frm.strftime("%Y-%m-%d"), "to": to.strftime("%Y-%m-%d"), "token": key}).json()
            for it in data if isinstance(data, list) else []:
                ts = it.get("datetime") or 0
                items.append(dict(ticker=t, source=it.get("source", ""), title=it.get("headline", ""),
                                  summary=it.get("summary", ""), url=it.get("url", ""),
                                  published=_utc(datetime.fromtimestamp(ts, timezone.utc)), sentiment=None, relevance=None))
            log_fetch(conn, "finnhub", "company-news", t, "ok")
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "finnhub", "company-news", t, "error")
            errors.append("%s: %s" % (t, http_client.redact(e)))
        time.sleep(1.1)
    return items, errors


def process(items, cfg, terms=None):
    n = cfg["news"]
    terms = terms or {}
    stats = dict(raw=len(items), not_whitelisted=0, not_relevant=0)
    kept = []
    for i in items:
        if not i.get("title") or not source_allowed(i.get("source"), n["whitelist_sources"]):
            stats["not_whitelisted"] += 1
            continue
        if i.get("relevance") is None:                 # Finnhub: nilai sendiri dari teks
            rel = is_relevant(i["ticker"], "%s %s" % (i["title"], i.get("summary", "")), terms)
            if rel is False:
                stats["not_relevant"] += 1
                continue
        kept.append(dict(i))
    kept.sort(key=lambda i: i["published"])

    clusters = []  # dedup: URL sama atau judul mirip (>0.8) untuk ticker yang sama
    for it in kept:
        for c in clusters:
            if c["ticker"] == it["ticker"] and (
                c["url"] == it["url"]
                or difflib.SequenceMatcher(None, c["title"].lower(), it["title"].lower()).ratio() > 0.8
            ):
                c["_sources"].add(str(it["source"]).lower())
                break
        else:
            it["_sources"] = {str(it["source"]).lower()}
            clusters.append(it)

    out = []
    for c in clusters:
        text = "%s %s" % (c["title"], c.get("summary", ""))
        c["tier"] = "RUMOR-TERBATAS" if kw_match(n["rumor_patterns"], text) else "BERITA"
        c["material"] = int(kw_match(n["material_keywords"], c["title"]))
        c["n_outlets"] = len(c["_sources"])
        c["summary"] = (c.get("summary") or "")[:300]
        c["id"] = hashlib.sha1(("%s%s" % (c["ticker"], c["url"])).encode()).hexdigest()
        out.append(c)

    final = []  # batasi per ticker: material dulu, lalu terbaru
    for t in sorted({c["ticker"] for c in out}):
        group = sorted((c for c in out if c["ticker"] == t), key=lambda c: (c["material"], c["published"]), reverse=True)
        final += group[: n["max_items_per_ticker"]]
    return final, stats


def run(conn, cfg):
    tickers = all_tickers(cfg)
    a, ea = _finnhub_news(conn, cfg, tickers)
    b, eb = _av_news(conn, cfg, tickers)
    final, stats = process(a + b, cfg, company_terms(cfg))
    conn.executemany(
        "INSERT OR REPLACE INTO news (id, ticker, source, title, summary, url, published, sentiment, tier, n_outlets, "
        "material, relevance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c["id"], c["ticker"], c["source"], c["title"], c["summary"], c["url"], c["published"], c["sentiment"],
          c["tier"], c["n_outlets"], c["material"], c.get("relevance")) for c in final])
    conn.commit()
    res = dict(raw=stats["raw"], kept=len(final), dropped_source=stats["not_whitelisted"],
               dropped_relevance=stats["not_relevant"], rumor=sum(c["tier"] == "RUMOR-TERBATAS" for c in final),
               material=sum(c["material"] for c in final))
    if ea or eb:
        res["failed"] = (ea + eb)[:5]
        if not a and not b and len(ea) >= len(tickers):
            res["status"], res["msg"] = "error", "; ".join(ea[:2])
    return res


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
