"""Konfigurasi, koneksi SQLite (dengan migrasi skema), dan pencatatan kuota API."""
import os
import sqlite3
from pathlib import Path

import yaml
from dotenv import load_dotenv

import schema

# INVEST_ROOT dipakai tes otomatis untuk mengarahkan semua file ke folder sementara.
ROOT = Path(os.environ.get("INVEST_ROOT") or Path(__file__).resolve().parent.parent)
load_dotenv(ROOT / ".env")


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_conn(cfg=None):
    cfg = cfg or load_config()
    path = ROOT / (cfg.get("paths") or {}).get("database", "data/market.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    schema.migrate(conn)
    return conn


def core_tickers(cfg):
    return [x["ticker"] for x in cfg["portfolio"]["core"]]


def satellite_tickers(cfg):
    return [x["ticker"] for x in cfg["portfolio"]["satellite"]]


def all_tickers(cfg):
    return core_tickers(cfg) + satellite_tickers(cfg)


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
    return fetched_within(conn, source, endpoint, ticker, 0)


def fetched_within(conn, source, endpoint, ticker="", days=0):
    """True bila sudah sukses diambil dalam `days` hari terakhir (0 = hari ini saja)."""
    return conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE source=? AND endpoint=? AND ticker=? AND status='ok' "
        "AND date(ts)>=date('now','localtime',?)",
        (source, endpoint, ticker, "-%d days" % days),
    ).fetchone()[0] > 0


def last_ok(conn, source, endpoint, ticker=""):
    r = conn.execute("SELECT MAX(ts) FROM fetch_log WHERE source=? AND endpoint=? AND ticker=? AND status='ok'",
                     (source, endpoint, ticker)).fetchone()
    return r[0] if r else None


def budget_left(conn, cfg, source="alphavantage"):
    b = cfg["api_budget"][source]
    return b["daily_limit"] - b.get("reserve", 0) - requests_today(conn, source)


def meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
    conn.commit()
