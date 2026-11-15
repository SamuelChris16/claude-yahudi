import numpy as np
import pandas as pd
import pytest

import ledger
import snapshot
from conftest import TMP, bdays, seed, write_tx

D = bdays(30)


def state(rows):
    write_tx(rows)
    df, errs = ledger.load()
    assert errs == []
    return ledger.replay(df)


def test_average_cost_fee_and_realized_profit():
    st = state([(D[0], "SETOR", "CASH", 1000, 1, 0, 16000),
                (D[1], "BELI", "AAPL", 2, 100, 1, 16000),
                (D[2], "BELI", "AAPL", 2, 120, 1, 16500),
                (D[3], "JUAL", "AAPL", 1, 150, 2, 16500),
                (D[4], "DIVIDEN", "AAPL", 3, 1, 0, "")])
    p = st["positions"]["AAPL"]
    assert p["shares"] == pytest.approx(3) and p["cost"] == pytest.approx(442 * 0.75)     # modal rata-rata 110.5/lembar
    assert st["realized"] == pytest.approx(150 - 2 - 110.5)
    assert st["cash"] == pytest.approx(1000 - 201 - 241 + 148 + 3) and st["dividends"] == 3
    assert st["warnings"] == []
    frame = ledger.to_portfolio_frame(st)
    assert frame.loc[frame.ticker == "AAPL", "avg_cost_usd"].iloc[0] == pytest.approx(110.5)
    assert 16000 < frame.loc[frame.ticker == "AAPL", "kurs_beli"].iloc[0] < 16500


def test_buy_without_deposit_is_treated_as_deposit_and_warned():
    st = state([(D[1], "BELI", "VOO", 0.5, 600, 0, 16000)])
    assert st["cash"] == 0 and st["events"][0][3] == pytest.approx(300)                 # arus setoran tersirat
    assert any("tidak didahului SETOR" in w for w in st["warnings"])


def test_overselling_is_ignored_with_warning():
    st = state([(D[1], "BELI", "VOO", 1, 100, 0, 16000), (D[2], "JUAL", "VOO", 2, 100, 0, 16000)])
    assert st["positions"]["VOO"]["shares"] == 1 and any("melebihi kepemilikan" in w for w in st["warnings"])


def test_bad_rows_are_reported():
    write_tx([("13/11/2026", "BELI", "VOO", 1, 100, 0, 16000), (D[1], "BORONG", "VOO", 1, 100, 0, 16000),
              (D[2], "BELI", "VOO", -1, 100, 0, 16000), (D[3], "BELI", "VOO", 1, "", 0, 16000)])
    _, errs = ledger.load()
    assert len(errs) == 4


def test_init_from_existing_portfolio():
    pd.DataFrame([("VOO", 0.5, 520, "2026-10-05", 16500, ""), ("CASH", 15, 1, "2026-10-26", 16500, "")],
                 columns=["ticker", "shares", "avg_cost_usd", "buy_date", "kurs_beli", "catatan"]).to_csv(TMP / "portofolio.csv", index=False)
    assert "dibuat" in ledger.init_from_portfolio()
    df, errs = ledger.load()
    st = ledger.replay(df)
    assert errs == [] and st["positions"]["VOO"]["shares"] == 0.5 and st["cash"] == pytest.approx(15)
    assert "sudah ada" in ledger.init_from_portfolio()                                  # tidak menimpa


# ---------- drawdown nyata ----------
def series(conn, rows):
    return snapshot.series_from_ledger(conn, state(rows)["events"])


def test_weekly_deposits_are_not_counted_as_gains(conn):
    seed(conn, "VOO", [100.0] * 30)
    s = series(conn, [(D[0], "BELI", "VOO", 1, 100, 0, 16000), (D[5], "BELI", "VOO", 1, 100, 0, 16000),
                      (D[10], "BELI", "VOO", 3, 100, 0, 16000)])
    assert list(s.total.iloc[[0, 5, 10]]) == [100, 200, 500]
    assert s.idx.tolist() == pytest.approx([1.0] * 30)
    assert snapshot.drawdown(s)["drawdown_pct"] == 0 and snapshot.drawdown(s)["return_pct"] == 0


def test_selling_at_a_profit_is_not_a_loss(conn):
    seed(conn, "VOO", [100.0] * 30)
    s = series(conn, [(D[0], "BELI", "VOO", 5, 80, 0, 16000), (D[10], "JUAL", "VOO", 1, 100, 0, 16000)])
    assert s.total.tolist() == pytest.approx([500.0] * 30)                               # nilai pindah ke kas
    assert snapshot.drawdown(s)["max_drawdown_pct"] == 0


def test_real_price_drop_shows_up_exactly(conn):
    px = [100.0] * 10 + [90.0] * 10 + [95.0] * 10
    seed(conn, "VOO", px)
    s = series(conn, [(D[0], "BELI", "VOO", 2, 100, 0, 16000), (D[12], "BELI", "VOO", 2, 90, 0, 16000)])
    d = snapshot.drawdown(s)
    assert d["max_drawdown_pct"] == pytest.approx(-10.0) and d["drawdown_pct"] == pytest.approx(-5.0)
    assert d["return_pct"] == pytest.approx(-5.0)                                        # time-weighted


def test_weekend_transaction_lands_on_next_trading_day_and_withdrawal_is_a_flow(conn):
    seed(conn, "VOO", [100.0] * 30)
    sat = (pd.Timestamp(D[4]) + pd.offsets.Week(weekday=5)).strftime("%Y-%m-%d")
    s = series(conn, [(D[0], "SETOR", "CASH", 500, 1, 0, 16000), (D[0], "BELI", "VOO", 2, 100, 0, 16000),
                      (sat, "TARIK", "CASH", 100, 1, 0, 16000)])
    nxt = s[s.date > sat].iloc[0]
    assert nxt.flow == -100 and nxt.total == 400
    assert s.idx.tolist() == pytest.approx([1.0] * 30)


def test_series_waits_for_prices_and_needs_five_days(conn):
    seed(conn, "VOO", [100.0] * 30)
    s = series(conn, [(D[27], "BELI", "VOO", 1, 100, 0, 16000)])
    assert len(s) == 3 and snapshot.drawdown(s) is None
    assert series(conn, [(D[0], "BELI", "MSFT", 1, 100, 0, 16000)]) is None             # MSFT belum punya harga
