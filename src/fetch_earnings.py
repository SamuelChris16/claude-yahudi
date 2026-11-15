"""Kalender earnings (Alpha Vantage, 1 request per minggu) untuk saham di portofolio."""
import csv
import io
import os

import requests

from db import get_conn, load_config, log_fetch, requests_today


def run(conn, cfg):
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not key:
        return {"status": "error", "msg": "ALPHAVANTAGE_API_KEY belum diisi"}
    if conn.execute("SELECT COUNT(*) FROM fetch_log WHERE source='alphavantage' AND endpoint='earnings' AND status='ok' "
                    "AND ts>=datetime('now','localtime','-6 days')").fetchone()[0]:
        return {"status": "cached"}
    b = cfg["api_budget"]["alphavantage"]
    if requests_today(conn, "alphavantage") >= b["daily_limit"] - b["reserve"]:
        return {"status": "skipped_budget"}
    r = requests.get("https://www.alphavantage.co/query",
                     params={"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": key}, timeout=60)
    text = r.text
    head = text.splitlines()[0] if text.strip() else ""
    if "symbol" not in head:
        log_fetch(conn, "alphavantage", "earnings", "ALL", "limited")
        return {"status": "error", "msg": text[:120]}
    core = {x["ticker"] for x in cfg["portfolio"]["core"]}
    mine = {x["ticker"] for x in cfg["portfolio"]["satellite"]} - core
    rows = [(x["symbol"], x["reportDate"], x.get("estimate") or None)
            for x in csv.DictReader(io.StringIO(text)) if x["symbol"] in mine]
    conn.execute("DELETE FROM earnings")
    conn.executemany("INSERT OR REPLACE INTO earnings VALUES (?,?,?)", rows)
    conn.commit()
    log_fetch(conn, "alphavantage", "earnings", "ALL", "ok")
    return {"status": "ok", "upcoming": [f"{t} {d}" for t, d, _ in sorted(rows, key=lambda x: x[1])]}


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
