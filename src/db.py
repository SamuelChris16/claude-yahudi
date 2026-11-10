"""Konfigurasi, koneksi SQLite, dan pencatatan kuota API."""
import sqlite3
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (ticker, date)
);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, ticker TEXT, source TEXT, title TEXT, summary TEXT, url TEXT,
    published TEXT, sentiment REAL, tier TEXT, n_outlets INTEGER, material INTEGER
);
CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY, ticker TEXT, form TEXT, filed TEXT, url TEXT, items TEXT
);
CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, endpoint TEXT, ticker TEXT,
    ts TEXT DEFAULT (datetime('now','localtime')), status TEXT
);
"""


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_conn(cfg=None):
    cfg = cfg or load_config()
    path = ROOT / cfg["paths"]["database"]
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def all_tickers(cfg):
    p = cfg["portfolio"]
    return [x["ticker"] for x in p["core"] + p["satellite"]]


def log_fetch(conn, source, endpoint, ticker, status):
    conn.execute(
        "INSERT INTO fetch_log (source, endpoint, ticker, status) VALUES (?,?,?,?)",
        (source, endpoint, ticker, status),
    )
    conn.commit()


def requests_today(conn, source):
    """Jumlah request hari ini (semua status) untuk menghitung kuota."""
    return conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE source=? AND date(ts)=date('now','localtime')",
        (source,),
    ).fetchone()[0]


def fetched_today(conn, source, endpoint, ticker=""):
    """True bila endpoint+ticker ini sudah sukses diambil hari ini (cache)."""
    return conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE source=? AND endpoint=? AND ticker=? "
        "AND status='ok' AND date(ts)=date('now','localtime')",
        (source, endpoint, ticker),
    ).fetchone()[0] > 0
