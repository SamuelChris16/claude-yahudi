"""Migrasi skema SQLite berversi (PRAGMA user_version) dan data contract per tabel.

Pakai:  python src/schema.py status   versi skema dan jumlah baris per tabel
        python src/schema.py check    periksa data contract (kolom wajib + aturan isi)
Migrasi berjalan otomatis setiap koneksi dibuka. Sebelum migrasi pada database yang sudah
berisi data, file disalin dulu ke <nama>.bak-v<versi lama>.
"""
import shutil
import sqlite3
import sys
from pathlib import Path

MIGRATIONS = [
    (1, "skema awal", """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, ticker TEXT, source TEXT, title TEXT, summary TEXT, url TEXT,
    published TEXT, sentiment REAL, tier TEXT, n_outlets INTEGER, material INTEGER);
CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY, ticker TEXT, form TEXT, filed TEXT, url TEXT, items TEXT);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    date TEXT PRIMARY KEY, invested REAL, cost REAL, cash REAL);
CREATE TABLE IF NOT EXISTS earnings (
    ticker TEXT, report_date TEXT, estimate TEXT, PRIMARY KEY (ticker, report_date));
CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, endpoint TEXT, ticker TEXT,
    ts TEXT DEFAULT (datetime('now','localtime')), status TEXT);
"""),
    (2, "harga mentah per sumber, aksi korporasi, kurs, makro, transaksi, isu data", """
CREATE TABLE prices_raw (
    ticker TEXT NOT NULL, date TEXT NOT NULL, source TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    fetched_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (ticker, date, source));
INSERT OR IGNORE INTO prices_raw (ticker, date, source, open, high, low, close, volume)
    SELECT ticker, date, 'alphavantage', open, high, low, close, volume FROM prices;
ALTER TABLE prices ADD COLUMN source TEXT;
ALTER TABLE prices ADD COLUMN adj_factor REAL DEFAULT 1.0;
UPDATE prices SET source='alphavantage', adj_factor=1.0;
CREATE TABLE corporate_actions (
    ticker TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL, ratio REAL NOT NULL,
    status TEXT NOT NULL, source TEXT, note TEXT, PRIMARY KEY (ticker, date, kind));
CREATE TABLE data_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now','localtime')),
    ticker TEXT, date TEXT, kind TEXT, severity TEXT, detail TEXT, UNIQUE (ticker, date, kind));
CREATE TABLE fx_rates (
    date TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL, rate REAL NOT NULL, source TEXT,
    PRIMARY KEY (date, base, quote));
CREATE TABLE macro (
    series_id TEXT NOT NULL, date TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY (series_id, date));
CREATE TABLE fundamentals_cache (
    ticker TEXT PRIMARY KEY, cik INTEGER, fetched_at TEXT, path TEXT, n_concepts INTEGER);
CREATE TABLE analyst_reco (
    ticker TEXT NOT NULL, period TEXT NOT NULL, strong_buy INTEGER, buy INTEGER, hold INTEGER,
    sell INTEGER, strong_sell INTEGER, PRIMARY KEY (ticker, period));
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, action TEXT NOT NULL, ticker TEXT NOT NULL,
    shares REAL NOT NULL, price_usd REAL, fee_usd REAL, fx_rate REAL, note TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
ALTER TABLE portfolio_snapshots ADD COLUMN total REAL;
ALTER TABLE portfolio_snapshots ADD COLUMN flow REAL;
ALTER TABLE portfolio_snapshots ADD COLUMN source TEXT;
UPDATE portfolio_snapshots SET total = invested + COALESCE(cash, 0), source = 'lama';
ALTER TABLE news ADD COLUMN relevance REAL;
"""),
]
LATEST = MIGRATIONS[-1][0]

# Data contract: kolom wajib + aturan isi. Tiap aturan = (penjelasan, SQL yang menghitung pelanggaran).
CONTRACT = {
    "prices_raw": dict(
        desc="Harga apa adanya per sumber. alphavantage = as-traded (belum disesuaikan split); "
             "stooq = sudah disesuaikan split dan dividen.",
        cols=["ticker", "date", "source", "open", "high", "low", "close", "volume"],
        rules=[("tanggal berformat YYYY-MM-DD", "SELECT COUNT(*) FROM prices_raw WHERE date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'")]),
    "prices": dict(
        desc="Harga bersih yang dipakai semua perhitungan: disesuaikan split, lolos validasi. "
             "Dibangun ulang oleh validate.build_clean; jangan diisi langsung.",
        cols=["ticker", "date", "open", "high", "low", "close", "volume", "source", "adj_factor"],
        rules=[("close > 0", "SELECT COUNT(*) FROM prices WHERE close IS NULL OR close <= 0"),
               ("high >= low", "SELECT COUNT(*) FROM prices WHERE high < low"),
               ("volume >= 0", "SELECT COUNT(*) FROM prices WHERE volume < 0"),
               ("tanggal berformat YYYY-MM-DD", "SELECT COUNT(*) FROM prices WHERE date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'")]),
    "corporate_actions": dict(
        desc="Split. status: terdeteksi (belum dipakai, menahan pemicu darurat), dikonfirmasi (dipakai), ditolak.",
        cols=["ticker", "date", "kind", "ratio", "status", "source", "note"],
        rules=[("status valid", "SELECT COUNT(*) FROM corporate_actions WHERE status NOT IN ('terdeteksi','dikonfirmasi','ditolak')"),
               ("rasio > 0", "SELECT COUNT(*) FROM corporate_actions WHERE ratio <= 0")]),
    "data_issues": dict(desc="Masalah kualitas data yang sedang aktif (dibangun ulang tiap validasi).",
                        cols=["ticker", "date", "kind", "severity", "detail"], rules=[]),
    "fx_rates": dict(desc="Kurs referensi harian (bukan kurs broker). rate = jumlah quote per 1 base.",
                     cols=["date", "base", "quote", "rate", "source"],
                     rules=[("rate > 0", "SELECT COUNT(*) FROM fx_rates WHERE rate <= 0")]),
    "macro": dict(desc="Deret makro FRED. Nilai kosong dari FRED tidak disimpan.",
                  cols=["series_id", "date", "value"], rules=[]),
    "transactions": dict(
        desc="Salinan transaksi.csv (sumber kebenaran posisi). Dimuat ulang penuh tiap run.",
        cols=["date", "action", "ticker", "shares", "price_usd", "fee_usd", "fx_rate", "note"],
        rules=[("aksi valid", "SELECT COUNT(*) FROM transactions WHERE action NOT IN ('BELI','JUAL','SETOR','TARIK','DIVIDEN')"),
               ("lembar/jumlah > 0", "SELECT COUNT(*) FROM transactions WHERE shares <= 0")]),
    "portfolio_snapshots": dict(
        desc="Nilai portofolio harian. total = saham + kas; flow = setoran bersih pada hari itu.",
        cols=["date", "invested", "cost", "cash", "total", "flow", "source"], rules=[]),
    "news": dict(desc="Berita lolos whitelist dan relevansi. published dalam UTC. tier: BERITA / RUMOR-TERBATAS.",
                 cols=["id", "ticker", "source", "title", "summary", "url", "published", "sentiment", "tier",
                       "n_outlets", "material", "relevance"],
                 rules=[("tier valid", "SELECT COUNT(*) FROM news WHERE tier NOT IN ('BERITA','RUMOR-TERBATAS')")]),
    "filings": dict(desc="Filing SEC (FAKTA-RESMI).", cols=["accession", "ticker", "form", "filed", "url", "items"], rules=[]),
    "earnings": dict(desc="Jadwal rilis laporan keuangan.", cols=["ticker", "report_date", "estimate"], rules=[]),
    "fundamentals_cache": dict(desc="Indeks berkas companyfacts SEC yang tersimpan di data/sec/.",
                               cols=["ticker", "cik", "fetched_at", "path", "n_concepts"], rules=[]),
    "analyst_reco": dict(desc="Tren rekomendasi analis (Finnhub), per bulan.",
                         cols=["ticker", "period", "strong_buy", "buy", "hold", "sell", "strong_sell"], rules=[]),
    "fetch_log": dict(desc="Catatan tiap request API: dasar cache dan hitungan kuota.",
                      cols=["source", "endpoint", "ticker", "ts", "status"], rules=[]),
    "meta": dict(desc="Pasangan kunci-nilai internal.", cols=["key", "value"], rules=[]),
}


def _db_file(conn):
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return Path(path)
    return None


def migrate(conn):
    """Naikkan skema ke versi terbaru. Aman dijalankan berulang. Return (versi_lama, versi_baru)."""
    old = conn.execute("PRAGMA user_version").fetchone()[0]
    if old >= LATEST:
        return old, old
    has_data = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
    f = _db_file(conn)
    if has_data and f and f.exists():
        conn.commit()
        bak = f.with_name(f.name + ".bak-v%d" % old)
        if not bak.exists():
            shutil.copy2(f, bak)
    for ver, _desc, sql in MIGRATIONS:
        if ver <= old:
            continue
        try:
            conn.executescript("BEGIN;\n%s\nPRAGMA user_version=%d;\nCOMMIT;" % (sql, ver))
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    return old, LATEST


def check_contract(conn):
    """Return daftar pelanggaran (kosong = sehat)."""
    out = []
    for table, c in CONTRACT.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if not have:
            out.append("%s: tabel tidak ada" % table)
            continue
        missing = [x for x in c["cols"] if x not in have]
        if missing:
            out.append("%s: kolom hilang %s" % (table, missing))
            continue
        for desc, sql in c["rules"]:
            n = conn.execute(sql).fetchone()[0]
            if n:
                out.append("%s: %d baris melanggar aturan '%s'" % (table, n, desc))
    return out


if __name__ == "__main__":
    from db import get_conn
    conn = get_conn()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "check":
        v = check_contract(conn)
        print("Data contract: " + ("OK" if not v else "ADA MASALAH"))
        for x in v:
            print(" -", x)
    else:
        print("Versi skema:", conn.execute("PRAGMA user_version").fetchone()[0], "(terbaru %d)" % LATEST)
        for t, c in CONTRACT.items():
            print("  %-20s %7d baris  | %s" % (t, conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0], c["desc"][:70]))
