"""Nilai portofolio harian dan drawdown yang nyata (time-weighted, kebal terhadap setoran dan penjualan).

Dengan transaksi.csv: deret dihitung ulang penuh dari buku transaksi + harga bersih, jadi langsung
tersedia sejak transaksi pertama dan tetap benar setelah transaksi dikoreksi.
  indeks_t = indeks_{t-1} * total_t / (total_{t-1} + setoran_bersih_t),  total = nilai saham + kas
Setoran tidak dihitung sebagai untung; menjual saham hanya memindahkan nilai ke kas, bukan rugi.
Tanpa transaksi.csv dipakai cara lama (perkiraan dari perubahan modal), yang keliru saat ada penjualan.
"""
import pandas as pd


def series_from_ledger(conn, events):
    """events dari ledger.replay(). Return DataFrame [date,positions,cash,total,flow,idx] atau None."""
    if not events:
        return None
    tickers = sorted({t for _, d, _, _ in events for t in d})
    if tickers:
        q = "SELECT date, ticker, close FROM prices WHERE ticker IN (%s) ORDER BY date" % ",".join("?" * len(tickers))
        px = pd.read_sql(q, conn, params=tickers)
        if px.empty:
            return None
        px = px.pivot(index="date", columns="ticker", values="close").sort_index().ffill()
        dates = list(px.index)
    else:  # hanya kas: pakai tanggal harga apa pun yang ada
        dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM prices ORDER BY date")]
        px = pd.DataFrame(index=dates)
    rows, shares, cash, i, n = [], {}, 0.0, 0, len(events)
    for d in dates:
        flow, touched = 0.0, False
        while i < n and events[i][0] <= d:
            _, d_sh, cash, f = events[i]
            for t, x in d_sh.items():
                shares[t] = shares.get(t, 0.0) + x
            flow += f
            touched = True
            i += 1
        if i == 0:
            continue  # belum ada transaksi sampai tanggal ini
        held = {t: s for t, s in shares.items() if s > 1e-9}
        if any(t not in px.columns or pd.isna(px.at[d, t]) for t in held):
            if not rows:
                continue  # tunggu sampai semua saham yang dipegang punya harga
            break  # saham baru belum punya harga: deret berhenti di sini (metrics memberi peringatan)
        val = sum(s * float(px.at[d, t]) for t, s in held.items())
        rows.append((d, val, cash, val + cash, flow))
    return _finish(rows)


def _finish(rows):
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "positions", "cash", "total", "flow"])
    idx = [1.0]
    for k in range(1, len(df)):
        base = df.total[k - 1] + df.flow[k]
        idx.append(idx[-1] * (df.total[k] / base if base > 1e-9 else 1.0))
    df["idx"] = idx
    return df


def drawdown(df, min_days=5):
    if df is None or len(df) < min_days:
        return None
    dd = df.idx / df.idx.cummax() - 1
    return dict(days=len(df), since=df.date.iloc[0], drawdown_pct=round(float(dd.iloc[-1]) * 100, 2),
                max_drawdown_pct=round(float(dd.min()) * 100, 2), return_pct=round((float(df.idx.iloc[-1]) - 1) * 100, 2))


def store(conn, df):
    """Simpan deret ke portfolio_snapshots (dipakai dashboard nanti)."""
    conn.execute("DELETE FROM portfolio_snapshots")
    conn.executemany(
        "INSERT INTO portfolio_snapshots (date, invested, cost, cash, total, flow, source) VALUES (?,?,?,?,?,?,'transaksi')",
        [(r.date, r.positions, None, r.cash, r.total, r.flow) for r in df.itertuples()])
    conn.commit()


# ---------- cara lama (tanpa transaksi.csv) ----------
def save(conn, summary, cash, date_str):
    if not summary or not summary.get("invested_usd") or not date_str:
        return
    conn.execute(
        "INSERT OR REPLACE INTO portfolio_snapshots (date, invested, cost, cash, total, flow, source) VALUES (?,?,?,?,?,?,'lama')",
        (date_str, summary["invested_usd"], summary["cost_usd"], cash, summary["invested_usd"] + cash, None))
    conn.commit()


def real_drawdown(conn, min_days=5):
    rows = conn.execute("SELECT date, invested, cost FROM portfolio_snapshots WHERE source='lama' ORDER BY date").fetchall()
    if len(rows) < min_days:
        return None
    idx = [1.0]
    for (_, pv, pc), (_, v, c) in zip(rows, rows[1:]):
        base = pv + (c - pc)
        idx.append(idx[-1] * (v / base if base > 0 else 1.0))
    peak, dds = idx[0], []
    for x in idx:
        peak = max(peak, x)
        dds.append(x / peak - 1)
    return dict(days=len(rows), since=rows[0][0], drawdown_pct=round(dds[-1] * 100, 2),
                max_drawdown_pct=round(min(dds) * 100, 2), return_pct=round((idx[-1] - 1) * 100, 2))
