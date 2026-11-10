"""Orkestrasi harian: harga -> indikator -> berita -> SEC -> ringkasan.

Pakai:  python src/run_daily.py            (semua)
        python src/run_daily.py --no-news  (lewati berita, hemat kuota)
"""
import json
import sys
import traceback

import fetch_news
import fetch_prices
import fetch_sec
import indicators
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


def main():
    cfg = load_config()
    conn = get_conn(cfg)
    report = {"prices": step("harga", fetch_prices.run, conn, cfg)}
    report["indicators"] = step("indikator", indicators.run, conn, cfg)
    if "--no-news" not in sys.argv:
        report["news"] = step("berita", fetch_news.run, conn, cfg)
    report["sec"] = step("sec", fetch_sec.run, conn, cfg)
    report["quota_alphavantage_used_today"] = requests_today(conn, "alphavantage")

    out = ROOT / "data" / "indicators_latest.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n=== RINGKASAN ===")
    print("Harga   :", report["prices"])
    print("Berita  :", report.get("news", "dilewati"))
    print("SEC     :", report["sec"])
    print("Kuota AV:", report["quota_alphavantage_used_today"], "request hari ini")
    for t, v in report["indicators"].items():
        if v.get("emergency"):
            print(f"!! DARURAT {t}: {v['emergency']}")
        elif v.get("flags"):
            print(f"-  flag {t}: {v['flags']}")


if __name__ == "__main__":
    main()
