"""Indikator teknikal dan flag, dihitung lokal dari data harga di SQLite."""
import numpy as np
import pandas as pd

from db import all_tickers, get_conn, load_config


def rsi(close, n=14):
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + au / ad)


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _f(x, nd=2):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def compute(df, cfg):
    tc, em = cfg["technical"], cfg["risk"]["emergency"]
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"]
    out = {"date": df["date"].iloc[-1], "close": _f(c.iloc[-1]), "n_days": len(df)}

    for n in tc["sma"]:
        out[f"sma{n}"] = _f(c.rolling(n).mean().iloc[-1])
    ema = {n: c.ewm(span=n, adjust=False).mean() for n in tc["ema"]}
    for n, s in ema.items():
        out[f"ema{n}"] = _f(s.iloc[-1])

    out["rsi"] = _f(rsi(c, tc["rsi"]["period"]).iloc[-1], 1)
    m = tc["macd"]
    macd = c.ewm(span=m["fast"], adjust=False).mean() - c.ewm(span=m["slow"], adjust=False).mean()
    sig = macd.ewm(span=m["signal"], adjust=False).mean()
    out.update(macd=_f(macd.iloc[-1], 3), macd_signal=_f(sig.iloc[-1], 3), macd_hist=_f((macd - sig).iloc[-1], 3))

    bp, bs = tc["bollinger"]["period"], tc["bollinger"]["std"]
    mid, sd = c.rolling(bp).mean(), c.rolling(bp).std()
    up, lo = mid + bs * sd, mid - bs * sd
    out.update(bb_upper=_f(up.iloc[-1]), bb_lower=_f(lo.iloc[-1]),
               bb_pct_b=_f((c.iloc[-1] - lo.iloc[-1]) / (up.iloc[-1] - lo.iloc[-1]), 2))

    a = atr(df, tc["atr_period"]).iloc[-1]
    out["atr"], out["atr_pct"] = _f(a), _f(a / c.iloc[-1] * 100)
    out["vol_annual_pct"] = _f(c.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252) * 100, 1)
    out["rel_volume"] = _f(df["volume"].iloc[-1] / df["volume"].rolling(20).mean().iloc[-1])

    def ret(n):
        return _f((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 1) if len(c) > n else None

    out.update(ret_1w=ret(5), ret_1m=ret(21), ret_3m=ret(63))
    out["drawdown_52w_pct"] = _f((c.iloc[-1] / c.tail(252).max() - 1) * 100, 1)

    # ---- flag teknikal ----
    flags, emergency = [], []
    al = tc["alerts"]
    if al["price_below_sma200"] and out.get("sma200") and c.iloc[-1] < out["sma200"]:
        flags.append("HARGA_DI_BAWAH_SMA200")
    if out["rsi"] is not None:
        if out["rsi"] >= tc["rsi"]["overbought"]:
            flags.append("RSI_OVERBOUGHT")
        elif out["rsi"] <= tc["rsi"]["oversold"]:
            flags.append("RSI_OVERSOLD")
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    diff = (s50 - s200).dropna().tail(6)
    if len(diff) == 6:
        if al["golden_cross"] and diff.iloc[0] < 0 <= diff.iloc[-1]:
            flags.append("GOLDEN_CROSS")
        if al["death_cross"] and diff.iloc[0] > 0 >= diff.iloc[-1]:
            flags.append("DEATH_CROSS")

    # ---- pemicu darurat tingkat saham ----
    if len(c) > 5:
        drop5 = (c.iloc[-1] / c.iloc[-6] - 1) * 100
        out["drop_5d_pct"] = _f(drop5, 1)
        if drop5 <= em["stock_drop_5d_pct"]:
            emergency.append(f"TURUN_5H_{drop5:.1f}%")
    if out["drawdown_52w_pct"] is not None and out["drawdown_52w_pct"] <= em["stock_drawdown_pct"]:
        emergency.append(f"DRAWDOWN_{out['drawdown_52w_pct']}%")
    if out["rel_volume"] is not None and out["rel_volume"] < em["volume_drop_ratio"]:
        emergency.append("VOLUME_ANJLOK")

    out["flags"], out["emergency"] = flags, emergency
    return out


def run(conn, cfg):
    result = {}
    for t in all_tickers(cfg):
        df = pd.read_sql("SELECT * FROM prices WHERE ticker=? ORDER BY date", conn, params=(t,))
        if len(df) < 30:
            result[t] = {"error": f"data kurang ({len(df)} hari)"}
            continue
        result[t] = compute(df, cfg)
    return result


if __name__ == "__main__":
    import json
    cfg = load_config()
    print(json.dumps(run(get_conn(cfg), cfg), indent=2))
