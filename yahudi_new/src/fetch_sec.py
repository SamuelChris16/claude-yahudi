"""Filing resmi SEC EDGAR (8-K, Form 4) sebagai fakta tingkat 1. Tanpa API key."""
import json
import os
import time
from datetime import datetime, timedelta

import http_client
from db import ROOT, all_tickers, core_tickers, fetched_today, get_conn, load_config, log_fetch


def headers():
    ua = os.getenv("SEC_USER_AGENT")
    if not ua or "example.com" in ua:
        raise RuntimeError("SEC_USER_AGENT belum diisi di .env (format: 'Nama email@domain.com')")
    return {"User-Agent": ua}


def cik_map(h, max_age_days=30):
    """Peta ticker -> CIK. Disimpan lokal dan disegarkan tiap 30 hari."""
    path = ROOT / "data" / "company_tickers.json"
    stale = (not path.exists()) or (time.time() - path.stat().st_mtime > max_age_days * 86400)
    if stale:
        try:
            r = http_client.get("https://www.sec.gov/files/company_tickers.json", headers=h)
            json.loads(r.text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(r.text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            if not path.exists():
                raise
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for v in data.values():
        out[v["ticker"]] = v["cik_str"]
        out[v["ticker"].replace("-", ".")] = v["cik_str"]     # BRK-B di SEC = BRK.B di broker
    return out


def run(conn, cfg):
    try:
        h = headers()
        cik = cik_map(h)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "msg": http_client.redact(e)}
    s = cfg["sec"]
    delay = 1 / s["max_requests_per_second"]
    cutoff = (datetime.now() - timedelta(days=s["lookback_days"])).strftime("%Y-%m-%d")
    core = set(core_tickers(cfg))  # ETF dilewati
    summary = {"new_filings": 0, "skipped": [], "failed": []}
    for t in all_tickers(cfg):
        if t in core or t not in cik:
            summary["skipped"].append(t)
            continue
        if fetched_today(conn, "sec", "submissions", t):
            continue
        try:
            c = int(cik[t])
            rec = http_client.get("https://data.sec.gov/submissions/CIK%010d.json" % c, headers=h).json()["filings"]["recent"]
            for i, form in enumerate(rec["form"]):
                if form in s["forms"] and rec["filingDate"][i] >= cutoff:
                    acc = rec["accessionNumber"][i]
                    url = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (c, acc.replace("-", ""), rec["primaryDocument"][i])
                    items = rec["items"][i] if "items" in rec else ""
                    cur = conn.execute("INSERT OR IGNORE INTO filings VALUES (?,?,?,?,?,?)",
                                       (acc, t, form, rec["filingDate"][i], url, items))
                    summary["new_filings"] += cur.rowcount
            conn.commit()
            log_fetch(conn, "sec", "submissions", t, "ok")
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "sec", "submissions", t, "error")
            summary["failed"].append("%s: %s" % (t, http_client.redact(e)))
        time.sleep(delay)
    return summary


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
