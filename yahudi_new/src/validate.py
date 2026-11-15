"""Kualitas data: validasi config.yaml, validasi harga, deteksi split, dan pembangunan harga bersih.

Alur harga:  prices_raw (apa adanya per sumber)  ->  build_clean()  ->  prices (dipakai semua perhitungan)
  1. Baris rusak (harga <= 0, high < low, kosong) dibuang dan dicatat.
  2. Lompatan harga yang cocok dengan rasio split dicatat sebagai kandidat split.
     - Dikonfirmasi otomatis bila sumber kedua (Stooq, sudah disesuaikan) tidak menunjukkan lompatan itu.
     - Bila tidak bisa dikonfirmasi: status 'terdeteksi', harga TIDAK diubah, dan pemicu darurat saham itu
       ditahan sampai kamu memutuskan (lihat perintah di bawah).
  3. Riwayat panjang Stooq disambungkan di belakang data Alpha Vantage setelah diskalakan dan dicocokkan.

Pakai:  python src/validate.py                              periksa config + bangun ulang harga bersih
        python src/validate.py split daftar
        python src/validate.py split konfirmasi AAPL 2026-06-08 4     (4 = split 4:1; 0.1 = reverse 1:10)
        python src/validate.py split tolak AAPL 2026-06-08            (lompatan itu memang pergerakan harga)
"""
import json
import re
import sys

import numpy as np
import pandas as pd

from db import ROOT, all_tickers, get_conn, load_config

SPLIT_RATIOS = (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50)
SPLIT_TOL = 0.04          # toleransi kecocokan rasio
JUMP_PCT = 0.25           # |return harian| di atas ini dicatat sebagai lonjakan
RECON_MIN_OVERLAP = 20    # minimal hari tumpang-tindih untuk mencocokkan dua sumber
RECON_MAX_DEV = 0.03      # selisih maksimum (persentil 95) setelah diskalakan
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")


# ---------------- config ----------------
def validate_config(cfg):
    """Return (errors, warnings). Job harian berhenti bila errors tidak kosong."""
    err, warn = [], []
    for sec in ("portfolio", "risk", "technical", "news", "sec", "api_budget", "paths"):
        if not isinstance(cfg.get(sec), dict):
            err.append("bagian '%s' tidak ada di config.yaml" % sec)
    if err:
        return err, warn
    p, rk = cfg["portfolio"], cfg["risk"]
    entries = []
    for grp in ("core", "satellite"):
        if not isinstance(p.get(grp), list) or not p[grp]:
            err.append("portfolio.%s kosong" % grp)
            continue
        for e in p[grp]:
            t = str(e.get("ticker", ""))
            if not TICKER_RE.match(t):
                err.append("ticker '%s' tidak valid (huruf besar, maks 7 karakter)" % t)
            w = e.get("target_weight")
            if not isinstance(w, (int, float)) or not 0 < w <= 1:
                err.append("%s: target_weight harus di antara 0 dan 1" % t)
                w = 0
            if not e.get("sector"):
                err.append("%s: sector kosong" % t)
            entries.append((grp, t, float(w), e.get("sector")))
    tick = [t for _, t, _, _ in entries]
    for t in sorted({t for t in tick if tick.count(t) > 1}):
        err.append("ticker %s tercantum lebih dari sekali" % t)
    total = sum(w for _, _, w, _ in entries)
    if abs(total - 1.0) > 0.001:
        err.append("total target_weight = %.4f, seharusnya 1.0" % total)
    cap, scap = rk.get("max_weight_single_stock"), rk.get("max_weight_sector")
    for name, v in (("max_weight_single_stock", cap), ("max_weight_sector", scap)):
        if not isinstance(v, (int, float)) or not 0 < v <= 1:
            err.append("risk.%s harus di antara 0 dan 1" % name)
    if isinstance(cap, (int, float)):
        for g, t, w, _ in entries:
            if g == "satellite" and w > cap + 1e-9:
                err.append("%s: target %.0f%% melebihi batas per saham %.0f%%" % (t, w * 100, cap * 100))
    if isinstance(scap, (int, float)):
        sect = {}
        for g, _, w, s in entries:
            if g == "satellite":
                sect[s] = sect.get(s, 0) + w
        for s, w in sect.items():
            if w > scap + 1e-9:
                warn.append("sektor %s: total target %.0f%% melebihi batas sektor %.0f%%" % (s, w * 100, scap * 100))
    em = rk.get("emergency") or {}
    for k in ("stock_drop_5d_pct", "stock_drawdown_pct", "portfolio_drawdown_pct"):
        if not isinstance(em.get(k), (int, float)) or em[k] >= 0:
            err.append("risk.emergency.%s harus angka negatif" % k)
    if not isinstance(rk.get("rebalance_band_pp"), (int, float)) or rk["rebalance_band_pp"] <= 0:
        err.append("risk.rebalance_band_pp harus > 0")
    av = (cfg["api_budget"].get("alphavantage") or {})
    lim, res = av.get("daily_limit", 0), av.get("reserve", 0)
    if not lim or res < 0 or res >= lim:
        err.append("api_budget.alphavantage: daily_limit harus > reserve >= 0")
    else:
        need = len(tick) + int((cfg["news"].get("alphavantage_max_requests_per_day", 2))) + 1
        if need > lim - res:
            warn.append("perkiraan %d request Alpha Vantage per hari melebihi anggaran %d; sebagian ticker akan tertunda"
                        % (need, lim - res))
    if av.get("delay_seconds", 0) < 12:
        warn.append("api_budget.alphavantage.delay_seconds < 12 berisiko kena batas 5 request/menit")
    if not isinstance(p.get("weekly_savings_idr", 0), (int, float)) or p.get("weekly_savings_idr", 0) < 0:
        err.append("portfolio.weekly_savings_idr harus angka >= 0")
    # ticker saham harus dikenal SEC (ETF inti tidak diperiksa)
    f = ROOT / "data" / "company_tickers.json"
    if f.exists():
        try:
            known = {v["ticker"] for v in json.loads(f.read_text(encoding="utf-8")).values()}
            for g, t, _, _ in entries:
                if g == "satellite" and t.replace(".", "-") not in known and t not in known:
                    err.append("%s tidak ditemukan di daftar emiten SEC (salah ketik?)" % t)
        except Exception:  # noqa: BLE001
            warn.append("data/company_tickers.json tidak terbaca; ticker belum diperiksa")
    else:
        warn.append("daftar emiten SEC belum diunduh; ticker baru diperiksa setelah fetch_sec berjalan sekali")
    return err, warn


# ---------------- harga ----------------
def _raw(conn, ticker, source):
    return pd.read_sql("SELECT date, open, high, low, close, volume FROM prices_raw WHERE ticker=? AND source=? ORDER BY date",
                       conn, params=(ticker, source))


def _sanitize(df, source, issues):
    """Buang baris rusak. `issues` diisi (date, kind, severity, detail)."""
    if df.empty:
        return df
    bad = df.close.isna() | (df.close <= 0)
    for d in df.date[bad]:
        issues.append((d, "harga_tidak_valid", "PERINGATAN", "%s: close kosong/<= 0, baris dibuang" % source))
    df = df[~bad].copy()
    for c in ("open", "high", "low"):
        df[c] = df[c].where(df[c] > 0, df.close)      # O/H/L kosong: pakai close, tidak membuang baris
    hl = df.high < df.low
    for d in df.date[hl]:
        issues.append((d, "high_di_bawah_low", "INFO", "%s: high < low, ditukar" % source))
    df.loc[hl, ["high", "low"]] = df.loc[hl, ["low", "high"]].values
    df["volume"] = df.volume.fillna(0).clip(lower=0)
    return df.drop_duplicates("date", keep="last").reset_index(drop=True)


def detect_splits(df, tol=SPLIT_TOL):
    """Cari lompatan close yang cocok dengan rasio split. Return [(date, ratio)], ratio > 1 = split biasa."""
    out, c = [], df.close.values
    for i in range(1, len(c)):
        up, dn = c[i - 1] / c[i], c[i] / c[i - 1]
        best = None
        for r in SPLIT_RATIOS:
            for val, ratio in ((up, float(r)), (dn, 1.0 / r)):
                dev = abs(val / r - 1)
                if dev <= tol and (best is None or dev < best[0]):
                    best = (dev, ratio)
        if best:
            out.append((df.date.iloc[i], best[1]))
    return out


def apply_splits(df, splits):
    df = df.copy()
    df["adj_factor"] = 1.0
    for d, ratio in splits:
        m = df.date < d
        for c in ("open", "high", "low", "close"):
            df.loc[m, c] = df.loc[m, c] / ratio
        df.loc[m, "volume"] = df.loc[m, "volume"] * ratio
        df.loc[m, "adj_factor"] = df.loc[m, "adj_factor"] / ratio
    return df


def _second_source_says(st, prev_date, date):
    """'tanpa_lompatan' / 'ada_lompatan' / None bila Stooq tidak mencakup kedua tanggal."""
    s = st.set_index("date").close
    if prev_date not in s.index or date not in s.index:
        return None
    return "tanpa_lompatan" if abs(s[prev_date] / s[date] - 1) < 0.15 else "ada_lompatan"


def build_ticker(conn, ticker):
    issues = []
    av = _sanitize(_raw(conn, ticker, "alphavantage"), "alphavantage", issues)
    st = _sanitize(_raw(conn, ticker, "stooq"), "stooq", issues)
    info = dict(rows=0, sources=[], splits=[], pending=[])

    # --- split pada data as-traded ---
    known = {d: (ratio, status) for d, ratio, status in conn.execute(
        "SELECT date, ratio, status FROM corporate_actions WHERE ticker=? AND kind='split'", (ticker,))}
    if not av.empty:
        for d, ratio in detect_splits(av):
            if d in known:
                continue
            prev = av.date[av.date < d].iloc[-1]
            verdict = _second_source_says(st, prev, d) if not st.empty else None
            status = {"tanpa_lompatan": "dikonfirmasi", "ada_lompatan": "ditolak"}.get(verdict, "terdeteksi")
            src = "otomatis (dicocokkan dengan Stooq)" if verdict else "deteksi lompatan harga"
            conn.execute("INSERT OR IGNORE INTO corporate_actions VALUES (?,?,?,?,?,?,?)",
                         (ticker, d, "split", ratio, status, src, "close %s -> %s" % (
                             round(float(av.close[av.date == prev].iloc[0]), 2), round(float(av.close[av.date == d].iloc[0]), 2))))
            known[d] = (ratio, status)
        confirmed = sorted((d, r) for d, (r, s) in known.items() if s == "dikonfirmasi")
        info["splits"] = confirmed
        info["pending"] = sorted(d for d, (_, s) in known.items() if s == "terdeteksi")
        av = apply_splits(av, [x for x in confirmed if x[0] > av.date.iloc[0]])
        av["source"] = "alphavantage"
    for d in info["pending"]:
        issues.append((d, "split_perlu_konfirmasi", "PERINGATAN",
                       "lompatan harga mirip split rasio %g; pemicu darurat %s ditahan sampai dikonfirmasi/ditolak"
                       % (known[d][0], ticker)))

    # --- sambung riwayat panjang ---
    clean = av
    if not st.empty:
        st = st.assign(adj_factor=np.nan, source="stooq")
        if av.empty:
            clean = st
        else:
            ov = av.merge(st, on="date", suffixes=("_a", "_s"))
            if len(ov) < RECON_MIN_OVERLAP:
                issues.append(("", "rekonsiliasi_kurang_data", "INFO",
                               "hanya %d hari tumpang-tindih Alpha Vantage vs Stooq; riwayat panjang belum disambung" % len(ov)))
            else:
                ratio = ov.close_a / ov.close_s
                k = float(ratio.head(5).median())
                dev = float((ratio / k - 1).abs().quantile(0.95))
                if dev > RECON_MAX_DEV:
                    issues.append(("", "rekonsiliasi_gagal", "PERINGATAN",
                                   "Alpha Vantage dan Stooq berbeda sampai %.1f%% setelah diskalakan; riwayat panjang tidak disambung" % (dev * 100)))
                else:
                    old = st[st.date < av.date.iloc[0]].copy()
                    for c in ("open", "high", "low", "close"):
                        old[c] = old[c] * k
                    old["adj_factor"] = k
                    clean = pd.concat([old, av], ignore_index=True)
                    info["recon"] = dict(overlap=len(ov), scale=round(k, 5), max_dev_pct=round(dev * 100, 2))

    # --- anomali pada deret akhir ---
    if not clean.empty:
        clean = clean.sort_values("date").reset_index(drop=True)
        ret = clean.close.pct_change()
        pend = set(info["pending"])
        for i in clean.index[(ret.abs() > JUMP_PCT)]:
            if clean.date[i] not in pend:
                issues.append((clean.date[i], "lonjakan", "PERINGATAN", "close berubah %+.1f%% dalam sehari" % (ret[i] * 100)))
        d = clean.date.values
        for i in range(1, len(d)):
            gap = int(np.busday_count(d[i - 1], d[i]))
            if gap > 4:
                issues.append((d[i], "tanggal_hilang", "INFO", "%d hari kerja tanpa data sebelum tanggal ini" % (gap - 1)))
        conn.execute("DELETE FROM prices WHERE ticker=?", (ticker,))
        conn.executemany(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source, adj_factor) VALUES (?,?,?,?,?,?,?,?,?)",
            [(ticker, r.date, r.open, r.high, r.low, r.close, r.volume, r.source,
              None if r.adj_factor != r.adj_factor else r.adj_factor) for r in clean.itertuples()])
        info["rows"] = len(clean)
        info["sources"] = sorted(clean.source.unique())
        info["first"], info["last"] = clean.date.iloc[0], clean.date.iloc[-1]

    conn.execute("DELETE FROM data_issues WHERE ticker=?", (ticker,))
    conn.executemany("INSERT OR IGNORE INTO data_issues (ticker, date, kind, severity, detail) VALUES (?,?,?,?,?)",
                     [(ticker, d, k, s, x) for d, k, s, x in issues])
    conn.commit()
    info["issues"] = len(issues)
    return info


def build_clean(conn, tickers=None):
    tickers = tickers or [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM prices_raw ORDER BY ticker")]
    return {t: build_ticker(conn, t) for t in tickers}


def hold_tickers(conn):
    """Saham yang pemicu daruratnya ditahan karena ada kandidat split yang belum diputuskan."""
    return {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM corporate_actions WHERE status='terdeteksi'")}


def active_issues(conn, severities=("PERINGATAN",)):
    q = "SELECT ticker, date, kind, severity, detail FROM data_issues WHERE severity IN (%s) ORDER BY ticker, date" % ",".join(
        "?" * len(severities))
    return conn.execute(q, severities).fetchall()


def set_split(conn, ticker, date, status, ratio=None):
    row = conn.execute("SELECT ratio FROM corporate_actions WHERE ticker=? AND date=? AND kind='split'", (ticker, date)).fetchone()
    if row is None and ratio is None:
        if status != "ditolak":
            return "Tidak ada kandidat split %s %s. Untuk menambah split manual, sertakan rasionya." % (ticker, date)
        ratio = 1
    conn.execute("INSERT OR REPLACE INTO corporate_actions VALUES (?,?,?,?,?,?,?)",
                 (ticker, date, "split", float(ratio) if ratio else row[0], status, "manual", "diputuskan pengguna"))
    conn.commit()
    build_ticker(conn, ticker)
    return "%s %s: split %s. Harga bersih dibangun ulang." % (ticker, date, status)


if __name__ == "__main__":
    cfg = load_config()
    conn = get_conn(cfg)
    a = sys.argv[1:]
    if a[:1] == ["split"]:
        if a[1:2] == ["konfirmasi"] and len(a) >= 4:
            print(set_split(conn, a[2].upper(), a[3], "dikonfirmasi", a[4] if len(a) > 4 else None))
        elif a[1:2] == ["tolak"] and len(a) >= 4:
            print(set_split(conn, a[2].upper(), a[3], "ditolak"))
        else:
            rows = conn.execute("SELECT ticker, date, ratio, status, source, note FROM corporate_actions ORDER BY ticker, date").fetchall()
            print("Belum ada catatan split." if not rows else "\n".join("%s %s rasio %g [%s] %s | %s" % r for r in rows))
        sys.exit(0)
    errs, warns = validate_config(cfg)
    print("Config:", "OK" if not errs else "ERROR")
    for e in errs:
        print("  ERROR:", e)
    for w in warns:
        print("  peringatan:", w)
    for t, i in build_clean(conn).items():
        print("%-6s %5d baris %s..%s sumber=%s split=%s%s" % (
            t, i["rows"], i.get("first", "-"), i.get("last", "-"), ",".join(i["sources"]) or "-",
            ["%s x%g" % x for x in i["splits"]] or "-", ("  rekonsiliasi=%s" % i["recon"]) if "recon" in i else ""))
    rows = active_issues(conn, ("PERINGATAN", "INFO"))
    print("Isu data aktif: %d" % len(rows))
    for r in rows:
        print("  [%s] %s %s %s: %s" % (r[3], r[0], r[1], r[2], r[4]))
    missing = [t for t in all_tickers(cfg) if not conn.execute("SELECT 1 FROM prices WHERE ticker=? LIMIT 1", (t,)).fetchone()]
    if missing:
        print("Belum ada harga untuk:", ", ".join(missing))
