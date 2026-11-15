"""Metrik portofolio: bobot, sektor, P&L USD/IDR, drawdown, korelasi, likuiditas, alokasi tabungan.

Posisi dibaca dari transaksi.csv bila ada (disarankan), kalau tidak dari portofolio.csv.
Kurs: tabel fx_rates (Frankfurter) -> config portfolio.usd_idr_now -> kurs beli terakhir.
Tanpa request API.
"""
import json

import numpy as np
import pandas as pd

import fetch_fx
import ledger
import snapshot
from db import ROOT, get_conn, load_config


def load_positions(conn, cfg):
    """Return (df posisi, kas, frame portofolio, peringatan, state buku transaksi atau None)."""
    warn, state = [], None
    tx, errs = ledger.load()
    if errs:
        raise ValueError("transaksi.csv bermasalah: " + "; ".join(errs[:5]))
    if tx is not None:
        state = ledger.replay(tx)
        warn += state["warnings"]
        pf = ledger.to_portfolio_frame(state)
        ledger.sync_db(conn, tx)
        try:
            pf.to_csv(ROOT / "portofolio.csv", index=False)   # salinan baca-saja agar mudah dilihat
        except OSError:
            pass
    else:
        pf = pd.read_csv(ROOT / "portofolio.csv")
        warn.append("transaksi.csv belum ada: drawdown portofolio hanya perkiraan dan keliru bila ada penjualan "
                    "(jalankan: python src/ledger.py init)")
    pf["ticker"] = pf["ticker"].astype(str).str.strip().str.upper()
    cash = float(pd.to_numeric(pf.loc[pf.ticker == "CASH", "shares"], errors="coerce").fillna(0).sum())
    pos = pf[pf.ticker != "CASH"].copy()
    pos["shares"] = pos["shares"].astype(float)

    meta = {x["ticker"]: x for x in cfg["portfolio"]["core"] + cfg["portfolio"]["satellite"]}
    core = {x["ticker"] for x in cfg["portfolio"]["core"]}
    rows = []
    for t in meta:
        r = pos[pos.ticker == t]
        sh = float(r.shares.sum())
        cost = float((r.shares * r.avg_cost_usd).sum())
        last = conn.execute("SELECT close, date FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1", (t,)).fetchone()
        if last is None:
            warn.append("%s: belum ada data harga" % t)
        rows.append(dict(ticker=t, sector=meta[t]["sector"], is_core=t in core, target=meta[t]["target_weight"],
                         shares=sh, cost=cost, price=last[0] if last else np.nan))
    for t in sorted(set(pos.ticker) - set(meta)):
        warn.append("%s: ada di portofolio tapi tidak ada di config.yaml (tidak ikut dihitung)" % t)
    df = pd.DataFrame(rows)
    df["value"] = df.shares * df.price
    return df, cash, pf, warn, state


def fx_info(conn, cfg, pf):
    """Return (kurs sekarang, kurs beli rata-rata tertimbang, keterangan sumber)."""
    held = pf[(pf.ticker != "CASH") & pf.kurs_beli.notna()]
    basis = (held.shares.astype(float) * held.avg_cost_usd).sum() if len(held) else 0
    avg_buy = float((held.kurs_beli * held.shares.astype(float) * held.avg_cost_usd).sum() / basis) if basis > 0 else None
    live = fetch_fx.latest(conn)
    if live:
        return float(live[0]), avg_buy, "Frankfurter %s (kurs referensi)" % live[1]
    manual = cfg["portfolio"].get("usd_idr_now")
    if manual:
        return float(manual), avg_buy, "config usd_idr_now (manual)"
    k = pf.kurs_beli.dropna()
    return (float(k.iloc[-1]) if len(k) else None), avg_buy, "kurs beli terakhir (tidak akurat)"


def estimated_drawdown(conn, df):
    """Perkiraan cadangan: jumlah lembar SAAT INI atas riwayat harga (bukan riwayat transaksi asli)."""
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
    return dict(days=len(series), since=series.index[0], drawdown_pct=round(float(dd.iloc[-1]) * 100, 2),
                max_drawdown_pct=round(float(dd.min()) * 100, 2))


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
                out.append(dict(pair="%s-%s" % (a, b), corr=round(float(c.loc[a, b]), 2)))
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
    """Bagi dana ke posisi paling di bawah target, hormati batas bobot per saham.

    Bila dana melebihi seluruh selisih, sisanya dibagi menurut bobot target ke posisi yang masih
    punya ruang. Return (alokasi {ticker: usd}, sisa yang tidak teralokasi)."""
    if deploy_usd <= 0:
        return {}, 0.0
    v = df.value.fillna(0.0)
    total = float(v.sum()) + deploy_usd
    room = pd.Series(np.where(df.is_core, np.inf, max_stock * total), index=df.index) - v
    room = room.clip(lower=0).where(df.price.notna(), 0.0)          # tanpa harga: tidak dialokasikan
    want = np.minimum((df.target * total - v).clip(lower=0), room)
    if want.sum() >= deploy_usd:
        alloc = want / want.sum() * deploy_usd
    else:
        alloc = want.copy()
        rest = deploy_usd - float(want.sum())
        for _ in range(len(df) + 1):
            free = room - alloc
            w = df.target.where(free > 1e-9, 0.0)
            if rest <= 1e-9 or w.sum() <= 0:
                break
            add = np.minimum(rest * w / w.sum(), free.where(np.isfinite(free), rest))
            alloc = alloc + add
            rest -= float(add.sum())
    left = deploy_usd - float(alloc.sum())
    return {df.ticker[i]: round(float(a), 2) for i, a in alloc.items() if a > 0.005}, round(max(left, 0.0), 2)


def run(conn, cfg):
    rk = cfg["risk"]
    df, cash, pf, warn, state = load_positions(conn, cfg)
    invested = float(df.value.sum())
    out = {"warnings": warn, "emergency": [], "alerts": [], "position_source": "transaksi.csv" if state else "portofolio.csv"}
    if invested <= 0:
        out["warnings"].append("tidak ada posisi bernilai (cek transaksi/portofolio dan data harga)")
        return out

    df["weight"] = df.value / invested
    df["dev_pp"] = (df.weight - df.target) * 100
    df["pnl_usd"] = df.value - df.cost
    df["pnl_pct"] = np.where(df.cost > 0, df.pnl_usd / df.cost * 100, np.nan)

    fx_now, fx_buy, fx_src = fx_info(conn, cfg, pf)
    if "tidak akurat" in fx_src:
        out["warnings"].append("kurs sekarang tidak tersedia -> memakai kurs beli terakhir (P&L kurs tidak akurat)")
    cost_total = float(df.cost.sum())
    pnl_usd = invested - cost_total
    out["summary"] = dict(invested_usd=round(invested, 2), cash_usd=round(cash, 2), cost_usd=round(cost_total, 2),
                          pnl_usd=round(pnl_usd, 2), pnl_pct=round(pnl_usd / cost_total * 100, 2) if cost_total else None,
                          fx_source=fx_src)
    if state:
        out["summary"].update(realized_usd=round(state["realized"], 2), dividends_usd=round(state["dividends"], 2))
    if fx_now and fx_buy:
        # nilai = (saham + kas) x kurs sekarang; modal = (modal saham + kas) x kurs beli
        # total = efek harga saham (dinilai pada kurs beli) + efek kurs atas seluruh nilai
        val_idr = (invested + cash) * fx_now
        stock_eff = pnl_usd * fx_buy
        fx_eff = (invested + cash) * (fx_now - fx_buy)
        out["summary"].update(usd_idr_now=round(fx_now, 2), usd_idr_avg_buy=round(fx_buy), value_idr=round(val_idr),
                              pnl_idr_total=round(stock_eff + fx_eff), pnl_idr_from_stock=round(stock_eff),
                              pnl_idr_from_fx=round(fx_eff))

    # ---- bobot vs target ----
    out["positions"] = [dict(ticker=r.ticker, sector=r.sector, shares=round(r.shares, 4),
                             price=None if pd.isna(r.price) else round(r.price, 2),
                             value=round(r.value, 2) if pd.notna(r.value) else None,
                             weight_pct=None if pd.isna(r.weight) else round(r.weight * 100, 1),
                             target_pct=round(r.target * 100, 1), dev_pp=None if pd.isna(r.dev_pp) else round(r.dev_pp, 1),
                             pnl_pct=None if pd.isna(r.pnl_pct) else round(r.pnl_pct, 1)) for r in df.itertuples()]
    for r in df.itertuples():
        if pd.isna(r.dev_pp):
            continue
        if abs(r.dev_pp) > rk["rebalance_band_pp"]:
            out["alerts"].append("REBALANCE %s: %+.1f pp dari target (band ±%s)" % (r.ticker, r.dev_pp, rk["rebalance_band_pp"]))
        if not r.is_core and r.weight > rk["max_weight_single_stock"]:
            out["alerts"].append("BOBOT_SAHAM %s: %.1f%% > batas %.0f%%" % (r.ticker, r.weight * 100, rk["max_weight_single_stock"] * 100))

    # ---- sektor (ETF inti dikecualikan) ----
    sat = df[~df.is_core]
    sec = sat.groupby("sector").value.sum() / invested
    out["sector_weights_pct"] = {k: round(float(v) * 100, 1) for k, v in sec.items()}
    for k, v in sec.items():
        if v > rk["max_weight_sector"]:
            out["alerts"].append("BOBOT_SEKTOR %s: %.1f%% > batas %.0f%%" % (k, v * 100, rk["max_weight_sector"] * 100))

    # ---- drawdown portofolio ----
    dd, last_price = None, conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    if state:
        ser = snapshot.series_from_ledger(conn, state["events"])
        if ser is not None:
            snapshot.store(conn, ser)
            dd = snapshot.drawdown(ser)
            if dd:
                dd["source"] = "nyata, dari transaksi.csv (time-weighted)"
            if ser.date.iloc[-1] != last_price:
                out["warnings"].append("deret nilai portofolio berhenti di %s (ada saham tanpa harga sesudahnya)" % ser.date.iloc[-1])
    else:
        snapshot.save(conn, out["summary"], cash, last_price)
        dd = snapshot.real_drawdown(conn)
        if dd:
            dd["source"] = "snapshot harian cara lama (keliru bila ada penjualan)"
    if dd is None:
        dd = estimated_drawdown(conn, df)
        if dd:
            dd["source"] = "perkiraan dari lembar saat ini (riwayat belum 5 hari)"
            out["warnings"].append("drawdown portofolio masih perkiraan (riwayat portofolio belum 5 hari bursa)")
    out["portfolio_drawdown"] = dd
    is_estimate = bool(dd) and dd["source"].startswith("perkiraan")
    if dd and dd["drawdown_pct"] <= rk["emergency"]["portfolio_drawdown_pct"]:
        if is_estimate:   # perkiraan mencakup masa sebelum saham dibeli: jangan jadikan darurat
            out["alerts"].append("DRAWDOWN_PERKIRAAN %s%% (belum dijadikan darurat karena masih perkiraan)" % dd["drawdown_pct"])
        else:
            out["emergency"].append("PORTOFOLIO_DRAWDOWN %s%%" % dd["drawdown_pct"])

    # ---- korelasi & likuiditas ----
    out["high_correlation"] = correlations(conn, list(sat.ticker[sat.price.notna()]))
    out["liquidity"] = liquidity(conn, df)

    # ---- alokasi tabungan minggu ini ----
    fx = fx_now or fx_buy
    weekly_usd = cfg["portfolio"].get("weekly_savings_idr", 0) / fx if fx else 0.0
    if not fx:
        out["warnings"].append("kurs tidak diketahui: tabungan mingguan belum bisa dikonversi ke USD")
    deploy = weekly_usd + cash
    alloc, left = allocate(df, deploy, rk["max_weight_single_stock"])
    out["savings_allocation"] = dict(weekly_usd=round(weekly_usd, 2), cash_usd=round(cash, 2), deployable_usd=round(deploy, 2),
                                     fx_used=round(fx, 2) if fx else None, allocation_usd=alloc, unallocated_usd=left,
                                     note="saran mekanis (menutup selisih ke target); belum mempertimbangkan berita/teknikal")
    return out


if __name__ == "__main__":
    cfg = load_config()
    res = run(get_conn(cfg), cfg)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "metrics_latest.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(json.dumps(res, indent=2, default=str))
