"""Buku transaksi (transaksi.csv) sebagai sumber kebenaran posisi, kas, dan arus setoran.

Kolom transaksi.csv:
  tanggal,aksi,ticker,lembar,harga_usd,biaya_usd,kurs,catatan
  aksi  : BELI | JUAL | SETOR | TARIK | DIVIDEN
  BELI/JUAL      -> ticker saham, lembar (boleh pecahan), harga_usd per lembar, biaya_usd
  SETOR/TARIK    -> ticker CASH, lembar = jumlah USD, kurs = rupiah per USD saat itu
  DIVIDEN        -> ticker pembayar, lembar = jumlah USD bersih yang diterima
Aturan: metode biaya rata-rata; biaya beli masuk ke modal. Bila BELI melebihi kas tercatat,
kekurangannya dianggap setoran pada hari itu (jadi SETOR boleh tidak dicatat).

Pakai:  python src/ledger.py init      buat transaksi.csv dari portofolio.csv yang ada (sekali saja)
        python src/ledger.py sinkron   hitung ulang portofolio.csv dari transaksi.csv
        python src/ledger.py cek       tampilkan posisi, kas, dan peringatan
"""
import sys

import numpy as np
import pandas as pd

from db import ROOT

COLS = ["tanggal", "aksi", "ticker", "lembar", "harga_usd", "biaya_usd", "kurs", "catatan"]
ACTIONS = ("BELI", "JUAL", "SETOR", "TARIK", "DIVIDEN")
EPS = 1e-9


def path():
    return ROOT / "transaksi.csv"


def load(p=None):
    """Return (DataFrame, errors). DataFrame None bila berkas tidak ada."""
    p = p or path()
    if not p.exists():
        return None, []
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    errors = []
    missing = [c for c in COLS[:4] if c not in df.columns]
    if missing:
        return None, ["transaksi.csv: kolom hilang %s" % missing]
    for c in COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[COLS].copy()
    df["aksi"] = df["aksi"].str.strip().str.upper()
    df["ticker"] = df["ticker"].str.strip().str.upper()
    for c in ("lembar", "harga_usd", "biaya_usd", "kurs"):
        df[c] = pd.to_numeric(df[c].str.strip().replace("", np.nan), errors="coerce")
    df["biaya_usd"] = df["biaya_usd"].fillna(0.0)
    d = pd.to_datetime(df["tanggal"].str.strip(), format="%Y-%m-%d", errors="coerce")
    for i in df.index[d.isna()]:
        errors.append("baris %d: tanggal tidak valid (pakai YYYY-MM-DD)" % (i + 2))
    df["tanggal"] = d.dt.strftime("%Y-%m-%d")
    for i, r in df.iterrows():
        n = i + 2
        if r.aksi not in ACTIONS:
            errors.append("baris %d: aksi '%s' tidak dikenal %s" % (n, r.aksi, list(ACTIONS)))
        if not (r.lembar > 0):
            errors.append("baris %d: lembar/jumlah harus > 0" % n)
        if r.aksi in ("BELI", "JUAL") and not (r.harga_usd > 0):
            errors.append("baris %d: harga_usd harus > 0 untuk %s" % (n, r.aksi))
        if r.aksi in ("BELI", "JUAL") and r.ticker in ("", "CASH"):
            errors.append("baris %d: ticker saham wajib untuk %s" % (n, r.aksi))
        if r.biaya_usd < 0:
            errors.append("baris %d: biaya_usd tidak boleh negatif" % n)
    df = df.sort_values("tanggal", kind="stable").reset_index(drop=True)
    return df, errors


def replay(df, as_of=None):
    """Putar ulang transaksi. Return dict: positions, cash, realized, dividends, events, warnings.

    events = daftar (tanggal, {ticker: perubahan lembar}, kas sesudahnya, arus setoran eksternal).
    """
    pos, cash, realized, divs = {}, 0.0, 0.0, 0.0
    events, warns, implied = [], [], 0.0
    for r in df.itertuples():
        if as_of and r.tanggal > as_of:
            break
        d_sh, flow = {}, 0.0
        if r.aksi == "SETOR":
            cash += r.lembar
            flow = r.lembar
        elif r.aksi == "TARIK":
            if r.lembar > cash + 1e-6:
                warns.append("%s: TARIK $%.2f melebihi kas $%.2f" % (r.tanggal, r.lembar, cash))
            cash -= r.lembar
            flow = -r.lembar
        elif r.aksi == "DIVIDEN":
            cash += r.lembar
            divs += r.lembar
        elif r.aksi == "BELI":
            cost = r.lembar * r.harga_usd + r.biaya_usd
            if cost > cash + EPS:
                flow = cost - max(cash, 0.0)
                implied += flow
                cash += flow
            cash -= cost
            p = pos.setdefault(r.ticker, dict(shares=0.0, cost=0.0, idr=0.0, idr_known=0.0, first=r.tanggal))
            p["shares"] += r.lembar
            p["cost"] += cost
            if r.kurs > 0:
                p["idr"] += cost * r.kurs
                p["idr_known"] += cost
            d_sh[r.ticker] = r.lembar
        elif r.aksi == "JUAL":
            p = pos.get(r.ticker)
            if not p or r.lembar > p["shares"] + 1e-6:
                warns.append("%s: JUAL %s %.4f lembar melebihi kepemilikan %.4f (baris diabaikan)"
                             % (r.tanggal, r.ticker, r.lembar, p["shares"] if p else 0))
                continue
            frac = min(r.lembar / p["shares"], 1.0)
            basis = p["cost"] * frac
            proceeds = r.lembar * r.harga_usd - r.biaya_usd
            realized += proceeds - basis
            for k in ("cost", "idr", "idr_known"):
                p[k] *= (1 - frac)
            p["shares"] -= r.lembar
            cash += proceeds
            d_sh[r.ticker] = -r.lembar
            if p["shares"] < 1e-9:
                del pos[r.ticker]
        events.append((r.tanggal, d_sh, cash, flow))
    if implied > 0.005:
        warns.append("pembelian senilai $%.2f tidak didahului SETOR; dianggap setoran pada hari pembelian" % implied)
    if abs(cash) < 1e-6:
        cash = 0.0
    return dict(positions=pos, cash=cash, realized=realized, dividends=divs, events=events, warnings=warns)


def to_portfolio_frame(state, fx_fallback=None):
    """Bentuk yang sama dengan portofolio.csv lama, supaya modul lain tidak perlu tahu sumbernya."""
    rows = []
    for t, p in sorted(state["positions"].items()):
        kurs = p["idr"] / p["idr_known"] if p["idr_known"] > 0 else (fx_fallback or np.nan)
        rows.append(dict(ticker=t, shares=round(p["shares"], 6), avg_cost_usd=round(p["cost"] / p["shares"], 4),
                         buy_date=p["first"], kurs_beli=round(kurs, 2) if kurs == kurs else np.nan,
                         catatan="otomatis dari transaksi.csv"))
    kurs_list = [r["kurs_beli"] for r in rows if r["kurs_beli"] == r["kurs_beli"]]
    rows.append(dict(ticker="CASH", shares=round(state["cash"], 2), avg_cost_usd=1.0, buy_date="",
                     kurs_beli=round(float(np.mean(kurs_list)), 2) if kurs_list else (fx_fallback or np.nan),
                     catatan="otomatis dari transaksi.csv"))
    return pd.DataFrame(rows, columns=["ticker", "shares", "avg_cost_usd", "buy_date", "kurs_beli", "catatan"])


def sync_db(conn, df):
    conn.execute("DELETE FROM transactions")
    conn.executemany(
        "INSERT INTO transactions (date, action, ticker, shares, price_usd, fee_usd, fx_rate, note) VALUES (?,?,?,?,?,?,?,?)",
        [(r.tanggal, r.aksi, r.ticker, r.lembar, None if r.harga_usd != r.harga_usd else r.harga_usd, r.biaya_usd,
          None if r.kurs != r.kurs else r.kurs, r.catatan) for r in df.itertuples()])
    conn.commit()


def init_from_portfolio():
    if path().exists():
        return "transaksi.csv sudah ada, tidak ditimpa."
    src = ROOT / "portofolio.csv"
    if not src.exists():
        return "portofolio.csv tidak ditemukan."
    pf = pd.read_csv(src)
    rows = []
    for r in pf.itertuples():
        t = str(r.ticker).strip().upper()
        tgl = str(r.buy_date) if isinstance(r.buy_date, str) and r.buy_date.strip() else pd.Timestamp.today().strftime("%Y-%m-%d")
        if t == "CASH":
            rows.append((tgl, "SETOR", "CASH", r.shares, 1, 0, r.kurs_beli, "saldo kas awal"))
        else:
            rows.append((tgl, "BELI", t, r.shares, r.avg_cost_usd, 0, r.kurs_beli, "posisi awal dari portofolio.csv"))
    out = pd.DataFrame(rows, columns=COLS).sort_values("tanggal", kind="stable")
    out.to_csv(path(), index=False)
    return "transaksi.csv dibuat dari portofolio.csv (%d baris). Periksa isinya, lalu catat transaksi baru di sana." % len(out)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cek"
    if cmd == "init":
        print(init_from_portfolio())
        sys.exit(0)
    df, errs = load()
    if df is None and not errs:
        sys.exit("transaksi.csv belum ada. Jalankan: python src/ledger.py init")
    if errs:
        sys.exit("transaksi.csv bermasalah:\n - " + "\n - ".join(errs))
    st = replay(df)
    frame = to_portfolio_frame(st)
    if cmd == "sinkron":
        frame.to_csv(ROOT / "portofolio.csv", index=False)
        print("portofolio.csv diperbarui dari transaksi.csv.")
    print(frame.to_string(index=False))
    print("Laba/rugi terealisasi: $%.2f | dividen diterima: $%.2f" % (st["realized"], st["dividends"]))
    for w in st["warnings"]:
        print("PERINGATAN:", w)
