"""Orkestrasi harian: harga -> validasi -> kurs -> makro -> indikator -> metrik -> berita -> SEC -> lainnya.

Pakai:  python src/run_daily.py            (semua; buat laporan darurat otomatis bila ada pemicu)
        python src/run_daily.py --no-news  (lewati berita, hemat kuota)
Untuk penjadwalan gunakan src/daily_job.py (lengkap: notifikasi, jeda, backup, dll.)
"""
import json
import sys
import traceback

import fetch_analyst
import fetch_earnings
import fetch_fundamentals
import fetch_fx
import fetch_macro
import fetch_news
import fetch_prices
import fetch_sec
import http_client
import indicators
import metrics
import report as rpt
import triggers
import validate
from db import ROOT, get_conn, load_config, requests_today


def step(name, fn, *args):
    print("[%s] ..." % name, end=" ", flush=True)
    try:
        res = fn(*args)
        bad = isinstance(res, dict) and res.get("status") == "error"
        print("GAGAL: %s" % res.get("msg") if bad else "selesai")
        return res
    except Exception as e:  # noqa: BLE001
        print("GAGAL: %s" % http_client.redact(e))
        print(http_client.redact(traceback.format_exc()))
        return {"status": "error", "msg": http_client.redact("%s: %s" % (type(e).__name__, e))}


def pipeline(conn, cfg, news=True):
    rep = {"prices": step("harga", fetch_prices.run, conn, cfg)}
    rep["clean"] = step("validasi harga", validate.build_clean, conn)
    rep["fx"] = step("kurs", fetch_fx.run, conn, cfg)
    rep["macro"] = step("makro", fetch_macro.run, conn, cfg)
    rep["indicators"] = step("indikator", indicators.run, conn, cfg)
    rep["metrics"] = step("metrik", metrics.run, conn, cfg)
    if news:
        rep["news"] = step("berita", fetch_news.run, conn, cfg)
    rep["sec"] = step("sec", fetch_sec.run, conn, cfg)
    rep["earnings"] = step("earnings", fetch_earnings.run, conn, cfg)
    rep["fundamentals"] = step("fundamental", fetch_fundamentals.run, conn, cfg)
    rep["analyst"] = step("analis", fetch_analyst.run, conn, cfg)
    rep["quota_alphavantage_used_today"] = requests_today(conn, "alphavantage")
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "indicators_latest.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return rep


def usable(rep):
    """(indikator, metrik) bila keduanya berhasil dihitung, selain itu (None, None)."""
    ind, met = rep.get("indicators"), rep.get("metrics")
    if not isinstance(ind, dict) or ind.get("status") == "error" or not isinstance(met, dict) or "summary" not in met:
        return None, None
    return ind, met


def main():
    cfg = load_config()
    errs, warns = validate.validate_config(cfg)
    for w in warns:
        print("[config] peringatan:", w)
    if errs:
        sys.exit("config.yaml bermasalah, job dibatalkan:\n - " + "\n - ".join(errs))
    conn = get_conn(cfg)
    rep = pipeline(conn, cfg, news="--no-news" not in sys.argv)
    ind, m = usable(rep)
    if ind is not None:
        trig = triggers.collect(conn, ind, m)
        if triggers.has_emergency(trig):
            path, tok = rpt.build("darurat", conn, cfg, ind, m, trig)
            print("!!! PEMICU DARURAT -> laporan dibuat: %s (~%s token). Buka Claude dan minta analisis." % (path, tok))

    print("\n=== RINGKASAN ===")
    for label, key in (("Harga", "prices"), ("Kurs", "fx"), ("Makro", "macro"), ("Berita", "news"), ("SEC", "sec"),
                       ("Earnings", "earnings"), ("Fundamental", "fundamentals"), ("Analis", "analyst")):
        print("%-11s: %s" % (label, rep.get(key, "dilewati")))
    print("Kuota AV   :", rep["quota_alphavantage_used_today"], "request hari ini")
    for r in validate.active_issues(conn):
        print("[DATA] %s %s %s: %s" % (r[0], r[1], r[2], r[4]))
    met = rep["metrics"] if isinstance(rep["metrics"], dict) else {}
    if met.get("status") == "error":
        print("[METRIK GAGAL]", met.get("msg"))
    for k in ("emergency", "alerts", "warnings"):
        for x in met.get(k, []):
            print("[%s] %s" % (k.upper(), x))
    print("Alokasi    :", met.get("savings_allocation", {}).get("allocation_usd"))
    for t, v in (ind or {}).items():
        if v.get("emergency"):
            print("!! DARURAT %s: %s" % (t, v["emergency"]))
        elif v.get("flags"):
            print("-  flag %s: %s" % (t, v["flags"]))


if __name__ == "__main__":
    main()
