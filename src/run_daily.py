"""Orkestrasi harian: harga -> indikator -> metrik -> berita -> SEC -> earnings.

Pakai:  python src/run_daily.py            (semua; buat laporan darurat otomatis bila ada pemicu)
        python src/run_daily.py --no-news  (lewati berita, hemat kuota)
Untuk penjadwalan gunakan src/daily_job.py (lengkap: notifikasi, jeda, backup, dll.)
"""
import json
import sys
import traceback

import fetch_earnings
import fetch_news
import fetch_prices
import fetch_sec
import indicators
import metrics
import report as rpt
import triggers
from db import ROOT, get_conn, load_config, requests_today


def step(name, fn, *args):
    print(f"[{name}] ...", end=" ", flush=True)
    try:
        res = fn(*args)
        print("selesai")
        return res
    except Exception as e:  # noqa: BLE001
        print(f"GAGAL: {e}")
        traceback.print_exc()
        return {"status": "error", "msg": str(e)}


def pipeline(conn, cfg, news=True):
    rep = {"prices": step("harga", fetch_prices.run, conn, cfg)}
    rep["indicators"] = step("indikator", indicators.run, conn, cfg)
    rep["metrics"] = step("metrik", metrics.run, conn, cfg)
    if news:
        rep["news"] = step("berita", fetch_news.run, conn, cfg)
    rep["sec"] = step("sec", fetch_sec.run, conn, cfg)
    rep["earnings"] = step("earnings", fetch_earnings.run, conn, cfg)
    rep["quota_alphavantage_used_today"] = requests_today(conn, "alphavantage")
    out = ROOT / "data" / "indicators_latest.json"
    out.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return rep


def main():
    cfg = load_config()
    conn = get_conn(cfg)
    rep = pipeline(conn, cfg, news="--no-news" not in sys.argv)
    m = rep["metrics"]
    trig = triggers.collect(conn, rep["indicators"], m)
    if triggers.has_emergency(trig):
        path, tok = rpt.build("darurat", conn, cfg, rep["indicators"], m, trig)
        print(f"!!! PEMICU DARURAT -> laporan dibuat: {path} (~{tok} token). Buka Claude dan minta analisis.")

    print("\n=== RINGKASAN ===")
    print("Harga   :", rep["prices"])
    print("Berita  :", rep.get("news", "dilewati"))
    print("SEC     :", rep["sec"])
    print("Earnings:", rep["earnings"])
    print("Kuota AV:", rep["quota_alphavantage_used_today"], "request hari ini")
    for k in ("emergency", "alerts", "warnings"):
        for x in m.get(k, []):
            print(f"[{k.upper()}] {x}")
    print("Alokasi :", m.get("savings_allocation", {}).get("allocation_usd"))
    for t, v in rep["indicators"].items():
        if v.get("emergency"):
            print(f"!! DARURAT {t}: {v['emergency']}")
        elif v.get("flags"):
            print(f"-  flag {t}: {v['flags']}")


if __name__ == "__main__":
    main()
