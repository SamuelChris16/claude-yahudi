"""Filing resmi SEC EDGAR (8-K, Form 4) sebagai fakta tingkat 1. Tanpa API key."""
import json
import os
import time
from datetime import datetime, timedelta

import requests

from db import ROOT, all_tickers, fetched_today, get_conn, load_config, log_fetch


def _headers():
    ua = os.getenv("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("SEC_USER_AGENT belum diisi di .env (format: 'Nama email@domain.com')")
    return {"User-Agent": ua}


def _cik_map(headers):
    path = ROOT / "data" / "company_tickers.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=30)
        r.raise_for_status()
        path.write_text(r.text, encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {v["ticker"]: v["cik_str"] for v in data.values()}


def run(conn, cfg):
    try:
        headers = _headers()
    except RuntimeError as e:
        return {"status": "error", "msg": str(e)}
    s = cfg["sec"]
    delay = 1 / s["max_requests_per_second"]
    cutoff = (datetime.now() - timedelta(days=s["lookback_days"])).strftime("%Y-%m-%d")
    cik = _cik_map(headers)
    core = {x["ticker"] for x in cfg["portfolio"]["core"]}  # ETF dilewati
    summary = {"new_filings": 0, "skipped": [], "failed": []}

    for t in all_tickers(cfg):
        if t in core or t not in cik:
            summary["skipped"].append(t)
            continue
        if fetched_today(conn, "sec", "submissions", t):
            continue
        try:
            c = int(cik[t])
            r = requests.get(f"https://data.sec.gov/submissions/CIK{c:010d}.json", headers=headers, timeout=30)
            r.raise_for_status()
            rec = r.json()["filings"]["recent"]
            for i, form in enumerate(rec["form"]):
                if form in s["forms"] and rec["filingDate"][i] >= cutoff:
                    acc = rec["accessionNumber"][i]
                    url = (f"https://www.sec.gov/Archives/edgar/data/{c}/"
                           f"{acc.replace('-', '')}/{rec['primaryDocument'][i]}")
                    items = rec["items"][i] if "items" in rec else ""
                    cur = conn.execute("INSERT OR IGNORE INTO filings VALUES (?,?,?,?,?,?)",
                                       (acc, t, form, rec["filingDate"][i], url, items))
                    summary["new_filings"] += cur.rowcount
            conn.commit()
            log_fetch(conn, "sec", "submissions", t, "ok")
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "sec", "submissions", t, "error")
            summary["failed"].append(f"{t}: {e}")
        time.sleep(delay)
    return summary


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
