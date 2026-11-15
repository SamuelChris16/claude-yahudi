"""Fase 2: metrik portofolio (bobot, sektor, P&L, drawdown, korelasi, likuiditas, alokasi tabungan).

Membaca portofolio.csv + harga di SQLite. Tanpa request API.
Opsional di config.yaml (portfolio:)  ->  usd_idr_now: 16300   (kurs hari ini; bila kosong dipakai kurs_beli terakhir)
"""
import json

import numpy as np
import pandas as pd

import snapshot
from db import ROOT, get_conn, load_config


def load_positions(conn, cfg):
    pf = pd.read_csv(ROOT / "portofolio.csv")
    pf["ticker"] = pf["ticker"].str.strip().str.upper()
    cash = float(pf.loc[pf.ticker == "CASH", "shares"].sum())
    pos = pf[pf.ticker != "CASH"].copy()
    pos["shares"] = pos["shares"].astype(float)

    meta = {x["ticker"]: x for x in cfg["portfolio"]["core"] + cfg["portfolio"]["satellite"]}
    core = {x["ticker"] for x in cfg["portfolio"]["core"]}
    rows, warn = [], []
    for t in meta:
        r = pos[pos.ticker == t]
        sh = r.shares.sum()
        cost = (r.shares * r.avg_cost_usd).sum()
        last = conn.execute("SELECT close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1", (t,)).fetchone()
        if last is None:
            warn.append(f"{t}: belum ada data harga")
        rows.append(dict(ticker=t, sector=meta[t]["sector"], is_core=t in core, target=meta[t]["target_weight"],
                         shares=sh, cost=cost, price=last[0] if last else np.nan))
    for t in set(pos.ticker) - set(meta):
        warn.append(f"{t}: ada di portofolio.csv tapi tidak ada di config.yaml (diabaikan)")
    df = pd.DataFrame(rows)
    df["value"] = df.shares * df.price
    return df, cash, pf, warn


def fx_info(cfg, pf):
    held = pf[pf.ticker != "CASH"]
    avg_buy = float((held.kurs_beli * held.shares * held.avg_cost_usd).sum() /
                    max((held.shares * held.avg_cost_usd).sum(), 1e-9)) if len(held) else None
    now = cfg["portfolio"].get("usd_idr_now") or (float(pf.kurs_beli.iloc[-1]) if len(pf) else None)
    return now, avg_buy, bool(cfg["portfolio"].get("usd_idr_now"))


def portfolio_drawdown(conn, df):
    """Drawdown memakai jumlah lembar SAAT INI atas riwayat harga yang ada (bukan riwayat transaksi asli)."""
    series = None
    for _, r in df[df.shares > 0].iterrows():
        p = pd.read_sql("SELECT date, close FROM prices WHERE ticker=? ORDER BY date", conn, params=(r.ticker,))
        s = p.set_index("date")["close"] * r.shares
        series = s if series is None else series.add(s, fill_value=np.nan)
    if series is None:
        return None
    series = series.dropna()
    if len(series) < 5:
        return None
    dd = series / series.cummax() - 1
    return dict(days=len(series), peak=round(float(series.max()), 2), now=round(float(series.iloc[-1]), 2),
                drawdown_pct=round(float(dd.iloc[-1]) * 100, 2), max_drawdown_pct=round(float(dd.min()) * 100, 2))


def correlations(conn, tickers, win=60, thr=0.85):
    px = {}
    for t in tickers:
        p = pd.read_sql("SELECT date, close FROM prices WHERE ticker=? ORDER BY date", conn, params=(t,))
        if len(p) > 20:
            px[t] = p.set_index("date")["close"]
    if len(px) < 2:
        return []
    c = pd.DataFrame(px).pct_change().dropna().tail(win).corr()
    out = []
    for i, a in enumerate(c.columns):
        for b in c.columns[i + 1:]:
            if c.loc[a, b] >= thr:
                out.append(dict(pair=f"{a}-{b}", corr=round(float(c.loc[a, b]), 2)))
    return sorted(out, key=lambda x: -x["corr"])


def liquidity(conn, df, part=0.20):
    out = {}
    for _, r in df[df.shares > 0].iterrows():
        v = conn.execute("SELECT AVG(volume) FROM (SELECT volume FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 20)",
                         (r.ticker,)).fetchone()[0]
        if v:
            out[r.ticker] = dict(avg_vol_20d=int(v), pct_of_daily_vol=round(r.shares / v * 100, 6),
                                 days_to_sell=round(r.shares / (v * part), 4))
    return out


def allocate(df, deploy_usd, max_stock):
    """Bagi dana ke posisi paling di bawah target (water-filling), hormati batas bobot saham."""
    if deploy_usd <= 0:
        return {}
    invested = df.value.sum()
    total = invested + deploy_usd
    cap = pd.Series(np.where(df.is_core, np.inf, max_stock * total), index=df.index)
    want = (df.target * total - df.value).clip(lower=0)
    want = np.minimum(want, (cap - df.value).clip(lower=0))
    if want.sum() <= 0:                       # semua sudah di/atas target -> ikuti bobot target (ETF inti saja bila saham penuh)
        base = df.target.where(((cap - df.value) > 0))
        want = base.fillna(0)
    alloc = want / want.sum() * deploy_usd if want.sum() > 0 else want
    return {df.ticker[i]: round(float(a), 2) for i, a in alloc.items() if a > 0.005}


def run(conn, cfg):
    rk = cfg["risk"]
    df, cash, pf, warn = load_positions(conn, cfg)
    priced = df.price.notna()
    invested = float(df.value.sum())
    out = {"warnings": warn, "emergency": [], "alerts": []}
    if invested <= 0:
        out["warnings"].append("tidak ada posisi bernilai (cek portofolio.csv dan data harga)")
        return out

    df["weight"] = df.value / invested
    df["dev_pp"] = (df.weight - df.target) * 100
    df["pnl_usd"] = df.value - df.cost
    df["pnl_pct"] = np.where(df.cost > 0, df.pnl_usd / df.cost * 100, np.nan)

    fx_now, fx_buy, fx_manual = fx_info(cfg, pf)
    if not fx_manual:
        out["warnings"].append("usd_idr_now belum diisi di config.yaml -> P&L kurs memakai kurs_beli terakhir (tidak akurat)")
    cost_total = float(df.cost.sum())
    pnl_usd = invested - cost_total
    out["summary"] = dict(invested_usd=round(invested, 2), cash_usd=round(cash, 2), cost_usd=round(cost_total, 2),
                          pnl_usd=round(pnl_usd, 2), pnl_pct=round(pnl_usd / cost_total * 100, 2) if cost_total else None)
    if fx_now and fx_buy:
        val_idr, cost_idr = (invested + cash) * fx_now, cost_total * fx_buy + cash * fx_now
        out["summary"].update(usd_idr_now=fx_now, usd_idr_avg_buy=round(fx_buy), value_idr=round(val_idr),
                              pnl_idr_total=round(val_idr - cost_idr),
                              pnl_idr_from_stock=round(pnl_usd * fx_buy), pnl_idr_from_fx=round((invested + cash) * (fx_now - fx_buy)))

    # ---- bobot vs target ----
    out["positions"] = [dict(ticker=r.ticker, sector=r.sector, shares=round(r.shares, 4), price=None if pd.isna(r.price) else round(r.price, 2),
                             value=round(r.value, 2) if pd.notna(r.value) else None, weight_pct=round(r.weight * 100, 1),
                             target_pct=round(r.target * 100, 1), dev_pp=round(r.dev_pp, 1),
                             pnl_pct=None if pd.isna(r.pnl_pct) else round(r.pnl_pct, 1)) for r in df.itertuples()]
    for r in df.itertuples():
        if abs(r.dev_pp) > rk["rebalance_band_pp"]:
            out["alerts"].append(f"REBALANCE {r.ticker}: {r.dev_pp:+.1f} pp dari target (band ±{rk['rebalance_band_pp']})")
        if not r.is_core and r.weight > rk["max_weight_single_stock"]:
            out["alerts"].append(f"BOBOT_SAHAM {r.ticker}: {r.weight*100:.1f}% > batas {rk['max_weight_single_stock']*100:.0f}%")

    # ---- sektor (ETF inti dikecualikan) ----
    sat = df[~df.is_core]
    sec = (sat.groupby("sector").value.sum() / invested).round(4)
    out["sector_weights_pct"] = {k: round(v * 100, 1) for k, v in sec.items()}
    for k, v in sec.items():
        if v > rk["max_weight_sector"]:
            out["alerts"].append(f"BOBOT_SEKTOR {k}: {v*100:.1f}% > batas {rk['max_weight_sector']*100:.0f}%")

    # ---- drawdown portofolio ----
    last_date = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    snapshot.save(conn, out["summary"], cash, last_date)
    dd = snapshot.real_drawdown(conn)
    if dd is not None:
        dd["source"] = "snapshot harian (disesuaikan setoran)"
    else:
        dd = portfolio_drawdown(conn, df)
        if dd:
            dd["source"] = "estimasi dari lembar saat ini (snapshot belum cukup 5 hari)"
            out["warnings"].append("drawdown portofolio masih estimasi; menjadi nyata setelah 5 hari snapshot terkumpul")
    out["portfolio_drawdown"] = dd
    if dd and dd["drawdown_pct"] <= rk["emergency"]["portfolio_drawdown_pct"]:
        out["emergency"].append(f"PORTOFOLIO_DRAWDOWN {dd['drawdown_pct']}%")

    # ---- korelasi & likuiditas ----
    out["high_correlation"] = correlations(conn, list(sat.ticker[priced]))
    out["liquidity"] = liquidity(conn, df)

    # ---- alokasi tabungan minggu ini ----
    fx = fx_now or 16000
    weekly_usd = cfg["portfolio"].get("weekly_savings_idr", 0) / fx
    deploy = weekly_usd + cash
    out["savings_allocation"] = dict(weekly_usd=round(weekly_usd, 2), cash_usd=round(cash, 2), deployable_usd=round(deploy, 2),
                                     fx_used=fx, allocation_usd=allocate(df, deploy, rk["max_weight_single_stock"]),
                                     note="saran mekanis (menutup selisih ke target); belum mempertimbangkan berita/teknikal")
    return out


if __name__ == "__main__":
    cfg = load_config()
    res = run(get_conn(cfg), cfg)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "metrics_latest.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(json.dumps(res, indent=2, default=str))
