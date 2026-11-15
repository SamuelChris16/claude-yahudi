import pandas as pd
import pytest

import metrics
import triggers
from conftest import TMP, bdays, seed, walk, write_tx

D = bdays(100)


def small_cfg(cfg):
    cfg["portfolio"]["core"] = [dict(ticker="VOO", target_weight=0.55, sector="Broad Market")]
    cfg["portfolio"]["satellite"] = [dict(ticker=t, target_weight=0.15, sector=s)
                                     for t, s in (("AAPL", "Technology"), ("MSFT", "Technology"), ("JNJ", "Healthcare"))]
    cfg["risk"]["max_weight_single_stock"] = 0.20
    return cfg


def prices(conn, flat=False):
    for i, (t, p0) in enumerate((("VOO", 600), ("AAPL", 230), ("MSFT", 420), ("JNJ", 160))):
        seed(conn, t, [float(p0)] * 100 if flat else walk(100, p0, 0.008, seed_=i + 1))


def test_idr_pnl_parts_sum_to_total_and_use_live_fx(conn, cfg):
    prices(conn)
    write_tx([(D[60], "SETOR", "CASH", 400, 1, 0, 16000), (D[60], "BELI", "VOO", 0.5, 580, 0, 16000),
              (D[70], "BELI", "AAPL", 0.2, 220, 0, 16400)])
    conn.execute("INSERT INTO fx_rates VALUES (date('now'), 'USD', 'IDR', 18200, 'frankfurter')")
    m = metrics.run(conn, small_cfg(cfg))
    s = m["summary"]
    assert s["usd_idr_now"] == 18200 and "Frankfurter" in s["fx_source"]
    assert s["pnl_idr_total"] == pytest.approx(s["pnl_idr_from_stock"] + s["pnl_idr_from_fx"], abs=2)
    assert s["value_idr"] == pytest.approx((s["invested_usd"] + s["cash_usd"]) * 18200, rel=1e-4)
    assert s["cash_usd"] == pytest.approx(400 - 290 - 44) and m["position_source"] == "transaksi.csv"
    assert (TMP / "portofolio.csv").exists()                                # salinan baca-saja diperbarui
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM portfolio_snapshots WHERE source='transaksi'").fetchone()[0] == 40


def test_fx_falls_back_to_config_then_purchase_rate(conn, cfg):
    prices(conn)
    write_tx([(D[60], "BELI", "VOO", 0.5, 580, 0, 16000)])
    c = small_cfg(cfg)
    m = metrics.run(conn, c)
    assert m["summary"]["usd_idr_now"] == 16000 and any("kurs sekarang tidak tersedia" in w for w in m["warnings"])
    c["portfolio"]["usd_idr_now"] = 17000
    assert metrics.run(conn, c)["summary"]["usd_idr_now"] == 17000


def test_real_drawdown_from_ledger_triggers_emergency_but_estimate_does_not(conn, cfg):
    for t, p0 in (("AAPL", 230), ("MSFT", 420), ("JNJ", 160)):
        seed(conn, t, [float(p0)] * 100)
    seed(conn, "VOO", [600.0] * 80 + [520.0] * 20)                          # -13.3%
    c = small_cfg(cfg)
    write_tx([(D[50], "BELI", "VOO", 1, 600, 0, 16000)])                    # dibeli sebelum jatuh -> nyata
    m = metrics.run(conn, c)
    assert m["portfolio_drawdown"]["source"].startswith("nyata") and m["emergency"] == ["PORTOFOLIO_DRAWDOWN -13.33%"]
    write_tx([(D[97], "BELI", "VOO", 1, 520, 0, 16000)])                    # dibeli sesudah jatuh, riwayat 3 hari
    m = metrics.run(conn, c)
    assert m["portfolio_drawdown"]["source"].startswith("perkiraan") and m["emergency"] == []
    assert any(a.startswith("DRAWDOWN_PERKIRAAN") for a in m["alerts"])
    assert ("PERINGATAN", "PORTOFOLIO") == triggers.collect(conn, {}, m)[0][:2]


def test_legacy_portfolio_csv_still_works_with_warning(conn, cfg):
    prices(conn)
    pd.DataFrame([("VOO", 0.5, 520, "2026-10-05", 16500, ""), ("CASH", 15, 1, "2026-10-26", 16500, "")],
                 columns=["ticker", "shares", "avg_cost_usd", "buy_date", "kurs_beli", "catatan"]).to_csv(TMP / "portofolio.csv", index=False)
    m = metrics.run(conn, small_cfg(cfg))
    assert m["position_source"] == "portofolio.csv" and any("transaksi.csv belum ada" in w for w in m["warnings"])
    assert m["summary"]["cash_usd"] == 15


def test_broken_ledger_stops_metrics(conn, cfg):
    prices(conn)
    write_tx([("kemarin", "BELI", "VOO", 1, 100, 0, 16000)])
    with pytest.raises(ValueError, match="transaksi.csv bermasalah"):
        metrics.run(conn, small_cfg(cfg))


def frame(values, prices_=None):
    return pd.DataFrame(dict(ticker=["VOO", "AAPL", "MSFT"], is_core=[True, False, False], target=[0.55, 0.09, 0.09],
                             value=values, price=prices_ or [1.0, 1.0, 1.0]))


def test_allocation_fills_gaps_proportionally():
    alloc, left = metrics.allocate(frame([550.0, 20.0, 50.0]), 30.0, 0.10)
    assert sum(alloc.values()) == pytest.approx(30, abs=0.02) and left == 0
    assert alloc["AAPL"] > alloc["MSFT"] and "VOO" not in alloc             # VOO sudah di atas target


def test_allocation_surplus_respects_cap_and_goes_to_core():
    df = frame([550.0, 95.0, 95.0])
    alloc, left = metrics.allocate(df, 300.0, 0.10)
    total = 740 + 300
    assert sum(alloc.values()) + left == pytest.approx(300, abs=0.02)
    for t, v in (("AAPL", 95.0), ("MSFT", 95.0)):
        assert (v + alloc.get(t, 0)) / total <= 0.10 + 1e-6
    assert alloc["VOO"] > 250


def test_allocation_skips_tickers_without_price():
    alloc, _ = metrics.allocate(frame([550.0, 0.0, 60.0], [1.0, float("nan"), 1.0]), 50.0, 0.10)
    assert "AAPL" not in alloc
