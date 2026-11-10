"""Ambil harga harian dari Alpha Vantage dengan cache dan anggaran kuota."""
import os
import time

import requests

from db import all_tickers, fetched_today, get_conn, load_config, log_fetch, requests_today

URL = "https://www.alphavantage.co/query"


def _call(ticker, key, size):
    r = requests.get(
        URL,
        params={"function": "TIME_SERIES_DAILY", "symbol": ticker, "outputsize": size, "apikey": key},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _save(conn, ticker, series):
    rows = [
        (ticker, d, float(v["1. open"]), float(v["2. high"]), float(v["3. low"]),
         float(v["4. close"]), int(v["5. volume"]))
        for d, v in series.items()
    ]
    conn.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def run(conn, cfg):
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not key:
        return {"status": "error", "msg": "ALPHAVANTAGE_API_KEY belum diisi"}
    b = cfg["api_budget"]["alphavantage"]
    limit = b["daily_limit"] - b["reserve"]
    summary = {"fetched": [], "cached": [], "failed": [], "stopped_by_budget": False}

    for t in all_tickers(cfg):
        if fetched_today(conn, "alphavantage", "daily", t):
            summary["cached"].append(t)
            continue
        if requests_today(conn, "alphavantage") >= limit:
            summary["stopped_by_budget"] = True
            break
        have = conn.execute("SELECT COUNT(*) FROM prices WHERE ticker=?", (t,)).fetchone()[0]
        size = "full" if have < 200 else "compact"
        try:
            data = _call(t, key, size)
            # paket gratis kadang menolak 'full' -> coba 'compact'
            if "Time Series (Daily)" not in data and size == "full":
                log_fetch(conn, "alphavantage", "daily", t, "retry_compact")
                time.sleep(b["delay_seconds"])
                data = _call(t, key, "compact")
            if "Time Series (Daily)" in data:
                n = _save(conn, t, data["Time Series (Daily)"])
                log_fetch(conn, "alphavantage", "daily", t, "ok")
                summary["fetched"].append(f"{t}({n} baris)")
            else:
                log_fetch(conn, "alphavantage", "daily", t, "limited")
                summary["failed"].append(f"{t}: {str(data)[:120]}")
                break  # kemungkinan kena batas, berhenti
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "alphavantage", "daily", t, "error")
            summary["failed"].append(f"{t}: {e}")
        time.sleep(b["delay_seconds"])
    return summary


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
