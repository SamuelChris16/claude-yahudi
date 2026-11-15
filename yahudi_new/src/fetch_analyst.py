"""Tren rekomendasi analis dari Finnhub (paket gratis), disegarkan seminggu sekali.

Transaksi insider tidak diambil dari Finnhub (statusnya di paket gratis tidak pasti); sumbernya
tetap Form 4 SEC yang resmi.
"""
import os
import time

import http_client
from db import fetched_within, get_conn, load_config, log_fetch, satellite_tickers

URL = "https://finnhub.io/api/v1/stock/recommendation"


def parse(payload):
    out = []
    for x in payload if isinstance(payload, list) else []:
        try:
            out.append((x["period"], int(x["strongBuy"]), int(x["buy"]), int(x["hold"]), int(x["sell"]), int(x["strongSell"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def run(conn, cfg):
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return {"status": "skipped", "msg": "FINNHUB_API_KEY belum diisi"}
    out = {"updated": [], "cached": 0, "failed": []}
    for t in satellite_tickers(cfg):
        if fetched_within(conn, "finnhub", "recommendation", t, 6):
            out["cached"] += 1
            continue
        try:
            rows = parse(http_client.get(URL, params={"symbol": t, "token": key}, retries=1).json())
            conn.executemany("INSERT OR REPLACE INTO analyst_reco VALUES (?,?,?,?,?,?,?)", [(t,) + r for r in rows])
            conn.commit()
            log_fetch(conn, "finnhub", "recommendation", t, "ok")
            out["updated"].append("%s(%d bulan)" % (t, len(rows)))
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "finnhub", "recommendation", t, "error")
            msg = http_client.redact(e)
            out["failed"].append("%s: %s" % (t, msg))
            if "HTTP 401" in msg or "HTTP 403" in msg:
                return {"status": "skipped", "msg": "Finnhub menolak endpoint rekomendasi (%s); dilewati" % msg[:60]}
        time.sleep(1.1)
    return out


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
