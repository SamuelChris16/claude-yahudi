"""Deret makro dari FRED (key gratis: FRED_API_KEY). Dipakai sebagai konteks rezim pasar.

Daftar deret bisa diubah di config.yaml -> macro.series. Bawaan:
  DFF fed funds | DGS2, DGS10 yield 2 & 10 tahun | T10Y2Y kurva | CPIAUCSL inflasi | UNRATE pengangguran
  VIXCLS volatilitas | BAMLH0A0HYM2 spread kredit high-yield
"""
import os
import time
from datetime import date, timedelta

import http_client
from db import fetched_today, get_conn, load_config, log_fetch

URL = "https://api.stlouisfed.org/fred/series/observations"
DEFAULT = ["DFF", "DGS2", "DGS10", "T10Y2Y", "CPIAUCSL", "UNRATE", "VIXCLS", "BAMLH0A0HYM2"]


def parse(payload):
    out = []
    for o in (payload or {}).get("observations", []):
        try:
            out.append((o["date"], float(o["value"])))   # nilai kosong FRED berupa "." -> dilewati
        except (KeyError, TypeError, ValueError):
            continue
    return out


def run(conn, cfg):
    key = os.getenv("FRED_API_KEY")
    if not key:
        return {"status": "skipped", "msg": "FRED_API_KEY belum diisi; data makro dilewati"}
    series = ((cfg.get("macro") or {}).get("series")) or DEFAULT
    out = {"updated": [], "cached": [], "failed": []}
    for sid in series:
        if fetched_today(conn, "fred", "observations", sid):
            out["cached"].append(sid)
            continue
        last = conn.execute("SELECT MAX(date) FROM macro WHERE series_id=?", (sid,)).fetchone()[0]
        start = (date.fromisoformat(last) - timedelta(days=60)) if last else (date.today() - timedelta(days=5 * 365))
        try:
            rows = parse(http_client.get(URL, params={"series_id": sid, "api_key": key, "file_type": "json",
                                                      "observation_start": start.isoformat()}).json())
            conn.executemany("INSERT OR REPLACE INTO macro VALUES (?,?,?)", [(sid, d, v) for d, v in rows])
            conn.commit()
            log_fetch(conn, "fred", "observations", sid, "ok")
            out["updated"].append("%s(%d)" % (sid, len(rows)))
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "fred", "observations", sid, "error")
            out["failed"].append("%s: %s" % (sid, http_client.redact(e)))
        time.sleep(0.6)
    if out["failed"] and not out["updated"] and not out["cached"]:
        return {"status": "error", "msg": "; ".join(out["failed"])[:200]}
    return out


def latest(conn, series_id):
    return conn.execute("SELECT value, date FROM macro WHERE series_id=? ORDER BY date DESC LIMIT 1", (series_id,)).fetchone()


if __name__ == "__main__":
    cfg = load_config()
    conn = get_conn(cfg)
    print(run(conn, cfg))
    for s in ((cfg.get("macro") or {}).get("series")) or DEFAULT:
        print(" ", s, latest(conn, s))
