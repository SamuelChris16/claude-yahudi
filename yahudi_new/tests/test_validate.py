import copy
from datetime import datetime

import numpy as np
import pytest

import fetch_prices
import indicators
import validate
from conftest import bdays, put_raw, seed, walk

NY = fetch_prices.ZoneInfo("America/New_York")


# ---------- config ----------
def test_shipped_config_has_no_errors(cfg):
    errs, _ = validate.validate_config(cfg)
    assert errs == []


@pytest.mark.parametrize("mutate, expect", [
    (lambda c: c["portfolio"]["satellite"][0].update(target_weight=0.05), "total target_weight"),
    (lambda c: c["portfolio"]["satellite"].append(dict(c["portfolio"]["satellite"][0])), "lebih dari sekali"),
    (lambda c: c["portfolio"]["satellite"][0].update(ticker="aapl"), "tidak valid"),
    (lambda c: c["risk"].update(max_weight_single_stock=0.05), "melebihi batas per saham"),
    (lambda c: c["risk"]["emergency"].update(stock_drop_5d_pct=12), "harus angka negatif"),
    (lambda c: c["api_budget"]["alphavantage"].update(reserve=30), "daily_limit"),
    (lambda c: c.pop("news"), "bagian 'news'"),
])
def test_config_errors_are_caught(cfg, mutate, expect):
    c = copy.deepcopy(cfg)
    mutate(c)
    errs, _ = validate.validate_config(c)
    assert any(expect in e for e in errs), errs


# ---------- split ----------
def _with_split(n=100, ratio=2.0, at=90):
    px = walk(n, 200, 0.004)
    px[at:] = px[at:] / ratio
    return px


def test_unconfirmed_split_holds_emergencies_and_leaves_prices_alone(conn, cfg):
    px = _with_split()
    dates = seed(conn, "AAPL", px)
    assert conn.execute("SELECT date, ratio, status FROM corporate_actions").fetchall() == [(dates[90], 2.0, "terdeteksi")]
    assert validate.hold_tickers(conn) == {"AAPL"}
    stored = [r[0] for r in conn.execute("SELECT close FROM prices WHERE ticker='AAPL' ORDER BY date")]
    assert stored == pytest.approx(list(px))                              # tidak mengarang penyesuaian
    cfg["portfolio"]["satellite"] = [dict(ticker="AAPL", target_weight=0.45, sector="T")]
    cfg["portfolio"]["core"] = []
    res = indicators.run(conn, cfg)["AAPL"]
    assert res["emergency"] == [] and "SPLIT_PERLU_KONFIRMASI" in res["flags"]
    assert [r[2] for r in validate.active_issues(conn)] == ["split_perlu_konfirmasi"]


def test_confirmed_split_is_adjusted_and_no_false_emergency(conn, cfg):
    px = _with_split()
    dates = seed(conn, "AAPL", px, volume=1_000_000)
    print(validate.set_split(conn, "AAPL", dates[90], "dikonfirmasi"))
    close = [r[0] for r in conn.execute("SELECT close FROM prices WHERE ticker='AAPL' ORDER BY date")]
    assert max(abs(np.diff(close) / close[:-1])) < 0.05                   # lompatan hilang
    assert close[0] == pytest.approx(px[0] / 2)
    v0, f0 = conn.execute("SELECT volume, adj_factor FROM prices WHERE ticker='AAPL' ORDER BY date LIMIT 1").fetchone()
    assert (v0, f0) == (2_000_000, 0.5)
    assert validate.hold_tickers(conn) == set() and validate.active_issues(conn) == []
    cfg["portfolio"]["satellite"] = [dict(ticker="AAPL", target_weight=0.45, sector="T")]
    cfg["portfolio"]["core"] = []
    assert indicators.run(conn, cfg)["AAPL"]["emergency"] == []


def test_split_auto_confirmed_by_second_source(conn):
    px = _with_split()
    adj = px.copy()
    adj[:90] = adj[:90] / 2
    put_raw(conn, "AAPL", adj, source="stooq")
    seed(conn, "AAPL", px)
    assert conn.execute("SELECT status FROM corporate_actions").fetchall() == [("dikonfirmasi",)]
    assert validate.hold_tickers(conn) == set()


def test_real_crash_confirmed_by_second_source_still_triggers(conn, cfg):
    px = _with_split()                      # kedua sumber menunjukkan -50%: ini memang jatuh, bukan split
    put_raw(conn, "AAPL", px, source="stooq")
    seed(conn, "AAPL", px)
    assert conn.execute("SELECT status FROM corporate_actions").fetchall() == [("ditolak",)]
    cfg["portfolio"]["satellite"] = [dict(ticker="AAPL", target_weight=0.45, sector="T")]
    cfg["portfolio"]["core"] = []
    assert any(e.startswith("DRAWDOWN_") for e in indicators.run(conn, cfg)["AAPL"]["emergency"])
    assert "lonjakan" in [r[2] for r in validate.active_issues(conn)]


def test_user_can_reject_split_candidate(conn):
    dates = seed(conn, "AAPL", _with_split())
    validate.set_split(conn, "AAPL", dates[90], "ditolak")
    assert validate.hold_tickers(conn) == set()


def test_reverse_split_detected(conn):
    px = walk(60, 5, 0.004)
    px[40:] = px[40:] * 10
    seed(conn, "X", px)
    assert conn.execute("SELECT ratio FROM corporate_actions").fetchone()[0] == pytest.approx(0.1)


# ---------- baris rusak, lonjakan, tanggal hilang ----------
def test_bad_rows_are_dropped_and_logged(conn):
    px = list(walk(40))
    px[10], px[20] = 0.0, -5.0
    dates = put_raw(conn, "X", px)
    conn.execute("UPDATE prices_raw SET high=low*0.9 WHERE date=?", (dates[30],))
    validate.build_clean(conn, ["X"])
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE ticker='X'").fetchone()[0] == 38
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE high < low OR close <= 0").fetchone()[0] == 0
    kinds = [r[0] for r in conn.execute("SELECT kind FROM data_issues ORDER BY kind")]
    assert kinds.count("harga_tidak_valid") == 2 and "high_di_bawah_low" in kinds


def test_jump_and_missing_dates_are_flagged(conn):
    px = list(walk(60, 100, 0.003))
    px[30:] = [p * 1.32 for p in px[30:]]                      # +32% bukan rasio split
    dates = bdays(70)
    dates = dates[:45] + dates[55:]                            # 10 hari kerja hilang
    put_raw(conn, "X", px, dates=dates)
    validate.build_clean(conn, ["X"])
    kinds = {r[0] for r in conn.execute("SELECT kind FROM data_issues")}
    assert {"lonjakan", "tanggal_hilang"} <= kinds
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 0


def test_issues_are_rebuilt_not_accumulated(conn):
    px = list(walk(40))
    px[10] = 0.0
    seed(conn, "X", px)
    seed(conn, "X", px)
    assert conn.execute("SELECT COUNT(*) FROM data_issues").fetchone()[0] == 1


# ---------- rekonsiliasi & backfill ----------
def test_long_history_is_spliced_after_scaling(conn):
    full = walk(600, 100, 0.01)
    put_raw(conn, "X", full * 0.96, source="stooq")            # disesuaikan dividen: level 4% lebih rendah
    info = seed(conn, "X", full[-100:]) and validate.build_clean(conn, ["X"])["X"]
    assert info["rows"] == 600 and info["sources"] == ["alphavantage", "stooq"]
    assert info["recon"]["scale"] == pytest.approx(1 / 0.96, rel=1e-3)
    close = [r[0] for r in conn.execute("SELECT close FROM prices WHERE ticker='X' ORDER BY date")]
    assert close == pytest.approx(list(full), rel=1e-6)        # tersambung mulus, tanpa lompatan di sambungan
    assert validate.active_issues(conn) == []


def test_mismatched_sources_are_not_spliced(conn):
    full = walk(400, 100, 0.01)
    other = walk(400, 100, 0.01, seed_=7)                      # deret lain sama sekali
    put_raw(conn, "X", other, source="stooq")
    seed(conn, "X", full[-100:])
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE ticker='X'").fetchone()[0] == 100
    assert "rekonsiliasi_gagal" in [r[2] for r in validate.active_issues(conn)]


def test_stooq_only_ticker_is_usable(conn):
    seed(conn, "X", walk(300), source="stooq")
    assert conn.execute("SELECT COUNT(*), MIN(source) FROM prices").fetchone() == (300, "stooq")


# ---------- bar parsial ----------
def test_partial_bar_is_not_stored(conn):
    during = datetime(2026, 11, 13, 11, 0, tzinfo=NY)
    after = datetime(2026, 11, 13, 16, 30, tzinfo=NY)
    assert fetch_prices.is_complete_bar("2026-11-12", during)
    assert not fetch_prices.is_complete_bar("2026-11-13", during)
    assert fetch_prices.is_complete_bar("2026-11-13", after)
    bar = {"1. open": "1", "2. high": "2", "3. low": "0.5", "4. close": "1.5", "5. volume": "10"}
    n, skipped = fetch_prices.save(conn, "X", {"2026-11-12": bar, "2026-11-13": bar}, now=during)
    assert (n, skipped) == (1, 1)
    assert conn.execute("SELECT date FROM prices_raw").fetchall() == [("2026-11-12",)]


def test_alphavantage_response_classification():
    assert fetch_prices.classify({"Time Series (Daily)": {}}) == "ok"
    assert fetch_prices.classify({"Error Message": "Invalid API call"}) == "invalid"
    assert fetch_prices.classify({"Information": "rate limit"}) == "limited"
    assert fetch_prices.classify({"Note": "5 calls per minute"}) == "limited"


def test_low_volume_is_a_flag_not_an_emergency(conn, cfg):
    vols = [1_000_000] * 99 + [150_000]
    seed(conn, "AAPL", walk(100, 100, 0.003), volume=vols)
    cfg["portfolio"]["satellite"] = [dict(ticker="AAPL", target_weight=0.45, sector="T")]
    cfg["portfolio"]["core"] = []
    r = indicators.run(conn, cfg)["AAPL"]
    assert r["emergency"] == [] and "VOLUME_RENDAH" in r["flags"]
    assert r["dd_window_days"] == 100
