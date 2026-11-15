"""Kalender earnings (Alpha Vantage, 1 request per minggu) untuk saham di portofolio."""
import csv
import io
import os

import http_client
from db import budget_left, fetched_within, get_conn, load_config, log_fetch, satellite_tickers


def parse(text, mine):
    head = text.splitlines()[0] if text.strip() else ""
    if "symbol" not in head or "reportDate" not in head:
        return None
    return [(x["symbol"], x["reportDate"], x.get("estimate") or None)
            for x in csv.DictReader(io.StringIO(text)) if x.get("symbol") in mine and x.get("reportDate")]


def run(conn, cfg):
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not key:
        return {"status": "error", "msg": "ALPHAVANTAGE_API_KEY belum diisi"}
    if fetched_within(conn, "alphavantage", "earnings", "ALL", 6):
        return {"status": "cached"}
    if budget_left(conn, cfg) <= 0:
        return {"status": "skipped_budget"}
    try:
        text = http_client.get("https://www.alphavantage.co/query",
                               params={"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": key}, timeout=60).text
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "alphavantage", "earnings", "ALL", "error")
        return {"status": "error", "msg": http_client.redact(e)}
    rows = parse(text, set(satellite_tickers(cfg)))
    if rows is None:
        log_fetch(conn, "alphavantage", "earnings", "ALL", "limited")
        return {"status": "error", "msg": http_client.redact(text)[:120]}
    conn.execute("DELETE FROM earnings")
    conn.executemany("INSERT OR REPLACE INTO earnings VALUES (?,?,?)", rows)
    conn.commit()
    log_fetch(conn, "alphavantage", "earnings", "ALL", "ok")
    return {"status": "ok", "upcoming": ["%s %s" % (t, d) for t, d, _ in sorted(rows, key=lambda x: x[1])]}


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
