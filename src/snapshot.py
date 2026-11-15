"""Snapshot portofolio harian -> drawdown yang nyata (disesuaikan arus setoran/penarikan).

Indeks dihitung berantai: idx_t = idx_{t-1} * nilai_t / (nilai_{t-1} + perubahan_modal_t),
sehingga tabungan mingguan tidak dianggap sebagai keuntungan.
"""


def save(conn, summary, cash, date_str):
    if not summary or not summary.get("invested_usd") or not date_str:
        return
    conn.execute("INSERT OR REPLACE INTO portfolio_snapshots VALUES (?,?,?,?)",
                 (date_str, summary["invested_usd"], summary["cost_usd"], cash))
    conn.commit()


def real_drawdown(conn, min_days=5):
    rows = conn.execute("SELECT date, invested, cost FROM portfolio_snapshots ORDER BY date").fetchall()
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
