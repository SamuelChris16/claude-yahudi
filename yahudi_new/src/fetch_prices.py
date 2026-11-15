"""Ambil harga harian Alpha Vantage ke prices_raw, dengan cache, anggaran kuota, dan penyaring bar parsial.

Catatan sumber: paket gratis hanya memberi `compact` (100 hari) dan harganya as-traded (belum
disesuaikan split). Penyesuaian dilakukan di validate.build_clean; riwayat panjang dari backfill.py.
"""
import os
import time
from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

import http_client
from db import all_tickers, budget_left, fetched_today, get_conn, load_config, log_fetch

URL = "https://www.alphavantage.co/query"


def ny_now():
    return datetime.now(ZoneInfo("America/New_York")) if ZoneInfo else None


def is_complete_bar(date_str, now=None):
    """False untuk bar hari berjalan sebelum bursa AS tutup (volume dan close masih sementara)."""
    now = now or ny_now()
    if now is None:
        return True
    today = now.strftime("%Y-%m-%d")
    if date_str < today:
        return True
    return date_str == today and now.time() >= dtime(16, 20)


def classify(data):
    """ok | invalid (ticker salah) | limited (kuota/tarif) | unknown."""
    if "Time Series (Daily)" in data:
        return "ok"
    if "Error Message" in data:
        return "invalid"
    if "Note" in data or "Information" in data:
        return "limited"
    return "unknown"


def save(conn, ticker, series, now=None):
    rows, skipped = [], 0
    for d, v in series.items():
        if not is_complete_bar(d, now):
            skipped += 1
            continue
        try:
            rows.append((ticker, d, "alphavantage", float(v["1. open"]), float(v["2. high"]), float(v["3. low"]),
                         float(v["4. close"]), float(v["5. volume"])))
        except (KeyError, TypeError, ValueError):
            skipped += 1
    conn.executemany("INSERT OR REPLACE INTO prices_raw (ticker, date, source, open, high, low, close, volume) "
                     "VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows), skipped


def run(conn, cfg, tickers=None):
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not key:
        return {"status": "error", "msg": "ALPHAVANTAGE_API_KEY belum diisi"}
    delay = cfg["api_budget"]["alphavantage"]["delay_seconds"]
    summary = {"fetched": [], "cached": [], "failed": [], "stopped_by_budget": False}
    for t in tickers or all_tickers(cfg):
        if fetched_today(conn, "alphavantage", "daily", t):
            summary["cached"].append(t)
            continue
        if budget_left(conn, cfg) <= 0:
            summary["stopped_by_budget"] = True
            break
        try:
            data = http_client.get(URL, params={"function": "TIME_SERIES_DAILY", "symbol": t,
                                                "outputsize": "compact", "apikey": key}).json()
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "alphavantage", "daily", t, "error")
            summary["failed"].append("%s: %s" % (t, http_client.redact(e)))
            time.sleep(delay)
            continue
        kind = classify(data)
        if kind == "ok":
            n, skipped = save(conn, t, data["Time Series (Daily)"])
            log_fetch(conn, "alphavantage", "daily", t, "ok")
            summary["fetched"].append("%s(%d baris%s)" % (t, n, ", %d bar parsial dilewati" % skipped if skipped else ""))
        elif kind == "invalid":
            log_fetch(conn, "alphavantage", "daily", t, "invalid")
            summary["failed"].append("%s: ticker tidak dikenal Alpha Vantage" % t)   # lanjut ke ticker berikutnya
        else:
            log_fetch(conn, "alphavantage", "daily", t, "limited")
            summary["failed"].append("%s: %s" % (t, http_client.redact(str(data))[:120]))
            if kind == "limited":
                summary["stopped_by_budget"] = True
                break
        time.sleep(delay)
    return summary


if __name__ == "__main__":
    import validate
    cfg = load_config()
    conn = get_conn(cfg)
    print(run(conn, cfg))
    validate.build_clean(conn)
