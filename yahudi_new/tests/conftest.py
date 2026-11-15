"""Semua tes memakai folder sementara (INVEST_ROOT) dan tidak pernah menyentuh jaringan atau data aslimu."""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="invest_test_"))
shutil.copy(PROJECT / "config.yaml", TMP / "config.yaml")
os.environ["INVEST_ROOT"] = str(TMP)
os.environ["INVEST_OFFLINE"] = "1"
for k in ("ALPHAVANTAGE_API_KEY", "FINNHUB_API_KEY", "FRED_API_KEY", "STOOQ_API_KEY", "SEC_USER_AGENT",
          "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(k, None)
sys.path.insert(0, str(PROJECT / "src"))

import db  # noqa: E402
import schema  # noqa: E402
import validate  # noqa: E402

END = "2026-11-13"


@pytest.fixture
def cfg():
    return db.load_config()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    schema.migrate(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def clean_root():
    yield
    for name in ("transaksi.csv", "portofolio.csv"):
        (TMP / name).unlink(missing_ok=True)
    shutil.rmtree(TMP / "data", ignore_errors=True)
    shutil.rmtree(TMP / "reports", ignore_errors=True)


def bdays(n, end=END):
    return list(pd.bdate_range(end=end, periods=n).strftime("%Y-%m-%d"))


def put_raw(conn, ticker, closes, source="alphavantage", end=END, volume=1_000_000, dates=None):
    """Sisipkan deret harga mentah. closes: list/array; tanggal = hari kerja yang berakhir di `end`."""
    closes = list(closes)
    dates = dates or bdays(len(closes), end)
    vols = volume if isinstance(volume, (list, np.ndarray)) else [volume] * len(closes)
    conn.executemany(
        "INSERT OR REPLACE INTO prices_raw (ticker, date, source, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
        [(ticker, d, source, c, c * 1.01, c * 0.99, c, v) for d, c, v in zip(dates, closes, vols)])
    conn.commit()
    return dates


def seed(conn, ticker, closes, **kw):
    dates = put_raw(conn, ticker, closes, **kw)
    validate.build_clean(conn, [ticker])
    return dates


def walk(n, start=100.0, vol=0.01, seed_=1):
    rng = np.random.default_rng(seed_)
    return start * np.cumprod(1 + rng.normal(0, vol, n))


def write_tx(rows):
    """rows: list tuple (tanggal, aksi, ticker, lembar, harga_usd, biaya_usd, kurs)."""
    df = pd.DataFrame(rows, columns=["tanggal", "aksi", "ticker", "lembar", "harga_usd", "biaya_usd", "kurs"])
    df["catatan"] = ""
    df.to_csv(TMP / "transaksi.csv", index=False)
