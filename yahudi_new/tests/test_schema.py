import sqlite3

import schema
from conftest import TMP


def test_fresh_database_reaches_latest_and_passes_contract(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.LATEST
    assert schema.check_contract(conn) == []
    assert schema.migrate(conn) == (schema.LATEST, schema.LATEST)      # idempoten


def test_old_database_is_migrated_without_losing_data():
    path = TMP / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE prices (ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, PRIMARY KEY (ticker, date));
        CREATE TABLE news (id TEXT PRIMARY KEY, ticker TEXT, source TEXT, title TEXT, summary TEXT, url TEXT, published TEXT, sentiment REAL, tier TEXT, n_outlets INTEGER, material INTEGER);
        CREATE TABLE filings (accession TEXT PRIMARY KEY, ticker TEXT, form TEXT, filed TEXT, url TEXT, items TEXT);
        CREATE TABLE portfolio_snapshots (date TEXT PRIMARY KEY, invested REAL, cost REAL, cash REAL);
        CREATE TABLE earnings (ticker TEXT, report_date TEXT, estimate TEXT, PRIMARY KEY (ticker, report_date));
        CREATE TABLE fetch_log (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, endpoint TEXT, ticker TEXT, ts TEXT DEFAULT (datetime('now','localtime')), status TEXT);
        INSERT INTO prices VALUES ('AAPL','2026-11-12',1,2,0.5,1.5,100), ('AAPL','2026-11-13',1,2,0.5,1.6,120);
        INSERT INTO news VALUES ('x','AAPL','CNBC','judul','','http://u','2026-11-12T10:00:00',0.1,'BERITA',1,0);
        INSERT INTO portfolio_snapshots VALUES ('2026-11-13', 400, 300, 15);
        INSERT INTO fetch_log (source, endpoint, ticker, status) VALUES ('alphavantage','daily','AAPL','ok');
    """)
    old.commit()
    assert schema.migrate(old) == (0, schema.LATEST)
    assert old.execute("SELECT COUNT(*), MAX(close) FROM prices").fetchone() == (2, 1.6)
    assert old.execute("SELECT COUNT(*) FROM prices_raw WHERE source='alphavantage'").fetchone()[0] == 2
    assert old.execute("SELECT total, source FROM portfolio_snapshots").fetchone() == (415, "lama")
    assert old.execute("SELECT title, relevance FROM news").fetchone() == ("judul", None)
    assert old.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0] == 1
    assert schema.check_contract(old) == []
    assert (TMP / "old.db.bak-v0").exists()                             # cadangan sebelum migrasi
    old.close()
    path.unlink()
    (TMP / "old.db.bak-v0").unlink()


def test_contract_reports_bad_rows(conn):
    conn.execute("INSERT INTO prices (ticker, date, open, high, low, close, volume) VALUES ('X','2026-01-02',1,1,2,0,5)")
    v = schema.check_contract(conn)
    assert any("close > 0" in x for x in v) and any("high >= low" in x for x in v)
