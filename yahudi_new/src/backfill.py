"""Backfill riwayat harga panjang dari Stooq (sekali jalan) supaya SMA200 dan backtest langsung bisa dipakai.

Data Stooq sudah disesuaikan split dan dividen. Disimpan apa adanya di prices_raw (source='stooq'),
lalu validate.build_clean menyambungkannya di belakang data Alpha Vantage setelah dicocokkan.

Sejak 2026 Stooq mewajibkan API key (gratis, lewat CAPTCHA):
  1. Buka https://stooq.com/q/d/?s=spy.us&get_apikey di browser, selesaikan CAPTCHA, salin key-nya.
  2. Tambahkan ke .env:  STOOQ_API_KEY=isi_key
  3. python src/backfill.py                 (semua ticker di config, 5 tahun)
     python src/backfill.py --tahun 10 --force
Tanpa key: unduh CSV tiap ticker secara manual dari halaman "Historical data" Stooq, simpan ke satu
folder dengan nama seperti aapl_us_d.csv atau aapl.us.txt, lalu:
     python src/backfill.py --folder data/stooq
"""
import argparse
import io
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import http_client
import validate
from db import all_tickers, get_conn, load_config, log_fetch
from fetch_prices import is_complete_bar

URL = "https://stooq.com/q/d/l/"


def stooq_symbol(ticker):
    return ticker.lower().replace(".", "-") + ".us"


def parse_csv(text):
    """Terima format unduhan per ticker (Date,Open,...) maupun format arsip (<TICKER>,<PER>,<DATE>,...)."""
    head = text.lstrip()[:300].lower()
    if not head:
        raise ValueError("respons kosong")
    if head.startswith("<!doctype") or head.startswith("<html") or "apikey" in head.split("\n")[0]:
        raise ValueError("Stooq meminta API key (lihat petunjuk di atas berkas backfill.py)")
    if "exceeded the daily hits limit" in head:
        raise ValueError("kuota harian Stooq habis, coba lagi besok")
    if head.startswith("no data"):
        raise ValueError("Stooq tidak punya data untuk simbol ini")
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().strip("<>").lower() for c in df.columns]
    if "vol" in df.columns:
        df = df.rename(columns={"vol": "volume"})
    need = ["date", "open", "high", "low", "close"]
    if any(c not in df.columns for c in need):
        raise ValueError("format CSV tidak dikenali: %s" % list(df.columns)[:8])
    if "volume" not in df.columns:
        df["volume"] = 0
    d = df["date"].astype(str).str.strip()
    d = pd.to_datetime(d, format="%Y%m%d", errors="coerce") if d.str.fullmatch(r"\d{8}").all() else pd.to_datetime(d, errors="coerce")
    df["date"] = d.dt.strftime("%Y-%m-%d")
    for c in need[1:] + ["volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    if df.empty:
        raise ValueError("tidak ada baris harga yang terbaca")
    return df[need + ["volume"]].sort_values("date").reset_index(drop=True)


def store(conn, ticker, df, since=None):
    if since:
        df = df[df.date >= since]
    df = df[[is_complete_bar(d) for d in df.date]]
    conn.executemany("INSERT OR REPLACE INTO prices_raw (ticker, date, source, open, high, low, close, volume) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     [(ticker, r.date, "stooq", r.open, r.high, r.low, r.close, r.volume) for r in df.itertuples()])
    conn.commit()
    return len(df)


def _find_file(folder, ticker):
    sym = stooq_symbol(ticker)
    for name in (sym + ".txt", sym + ".csv", sym.replace(".", "_") + "_d.csv", ticker.lower() + ".csv"):
        p = Path(folder) / name
        if p.exists():
            return p
    return None


def run(conn, cfg, tickers=None, years=5, force=False, folder=None, pause=2.0):
    tickers = tickers or all_tickers(cfg)
    since = (date.today() - timedelta(days=int(365.25 * years))).isoformat()
    key = os.getenv("STOOQ_API_KEY")
    out = {"filled": [], "skipped": [], "failed": []}
    if not folder and not key:
        return {"status": "error", "msg": "STOOQ_API_KEY belum diisi (atau pakai --folder untuk CSV unduhan manual)"}
    for t in tickers:
        have = conn.execute("SELECT COUNT(*) FROM prices_raw WHERE ticker=? AND source='stooq'", (t,)).fetchone()[0]
        if have >= 250 and not force:
            out["skipped"].append("%s (sudah %d baris)" % (t, have))
            continue
        try:
            if folder:
                f = _find_file(folder, t)
                if not f:
                    raise ValueError("berkas tidak ditemukan di %s" % folder)
                text = f.read_text(encoding="utf-8", errors="replace")
            else:
                text = http_client.get(URL, params={"s": stooq_symbol(t), "i": "d", "d1": since.replace("-", ""),
                                                    "d2": date.today().strftime("%Y%m%d"), "apikey": key}, timeout=60).text
            n = store(conn, t, parse_csv(text), since)
            log_fetch(conn, "stooq", "daily", t, "ok")
            out["filled"].append("%s (%d baris)" % (t, n))
        except Exception as e:  # noqa: BLE001
            log_fetch(conn, "stooq", "daily", t, "error")
            out["failed"].append("%s: %s" % (t, http_client.redact(e)))
            if "kuota harian" in str(e) or "API key" in str(e):
                break
        if not folder:
            time.sleep(pause)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tahun", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--folder")
    ap.add_argument("ticker", nargs="*")
    a = ap.parse_args()
    cfg = load_config()
    conn = get_conn(cfg)
    res = run(conn, cfg, [t.upper() for t in a.ticker] or None, a.tahun, a.force, a.folder)
    print(res)
    for t, i in validate.build_clean(conn).items():
        print("%-6s %5d baris %s..%s sumber=%s %s" % (t, i["rows"], i.get("first", "-"), i.get("last", "-"),
                                                     ",".join(i["sources"]), i.get("recon", "")))
    for r in validate.active_issues(conn):
        print("  [%s] %s %s %s: %s" % (r[3], r[0], r[1], r[2], r[4]))
