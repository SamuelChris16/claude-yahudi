"""Unduh dan simpan data fundamental mentah SEC XBRL (companyfacts) untuk screening.

Tahap ini hanya mengunduh dan menyimpan: data/sec/companyfacts/CIK##########.json.gz.
Perhitungan rasio dan skor dikerjakan modul screening (tahap berikutnya), secara lokal.
Semesta: saham satelit + daftar screening.universe di config.yaml. Disegarkan tiap 30 hari.
"""
import gzip
import json
import time
from datetime import datetime

import http_client
from db import ROOT, fetched_within, get_conn, load_config, log_fetch, satellite_tickers
from fetch_sec import cik_map, headers


def universe(cfg):
    extra = ((cfg.get("screening") or {}).get("universe")) or []
    seen, out = set(), []
    for t in satellite_tickers(cfg) + [str(x).upper() for x in extra]:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def run(conn, cfg, max_per_run=10, refresh_days=30):
    try:
        h = headers()
        ciks = cik_map(h)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "msg": http_client.redact(e)}
    delay = 1.0 / cfg["sec"]["max_requests_per_second"]
    folder = ROOT / "data" / "sec" / "companyfacts"
    folder.mkdir(parents=True, exist_ok=True)
    out = {"downloaded": [], "fresh": 0, "unknown": [], "failed": [], "pending": 0}
    todo = []
    for t in universe(cfg):
        if t not in ciks:
            out["unknown"].append(t)
        elif fetched_within(conn, "sec", "companyfacts", t, refresh_days):
            out["fresh"] += 1
        else:
            todo.append(t)
    out["pending"] = max(0, len(todo) - max_per_run)
    for t in todo[:max_per_run]:
        cik = int(ciks[t])
        try:
            r = http_client.get("https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % cik, headers=h, timeout=60)
            data = r.json()
            n = len((data.get("facts") or {}).get("us-gaap") or {})
            path = folder / ("CIK%010d.json.gz" % cik)
            with gzip.open(path, "wt", encoding="utf-8") as f:
                json.dump(data, f)
            conn.execute("INSERT OR REPLACE INTO fundamentals_cache VALUES (?,?,?,?,?)",
                         (t, cik, datetime.now().isoformat(timespec="seconds"), str(path.relative_to(ROOT)), n))
            conn.commit()
            log_fetch(conn, "sec", "companyfacts", t, "ok")
            out["downloaded"].append("%s(%d konsep)" % (t, n))
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "sec", "companyfacts", t, "error")
            out["failed"].append("%s: %s" % (t, http_client.redact(e)))
        time.sleep(delay)
    return out


if __name__ == "__main__":
    cfg = load_config()
    print(run(get_conn(cfg), cfg))
