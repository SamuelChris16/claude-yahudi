"""Uji ujung-ke-ujung tanpa jaringan dan tanpa API key: pipeline harus tetap selesai dan melapor dengan jujur."""
import os
import subprocess
import sys

import yaml

import daily_job
import db
import report
import run_daily
import triggers
import validate
from conftest import PROJECT, TMP, bdays, put_raw, walk, write_tx

D = bdays(300)
START = {"VOO": 600, "AAPL": 230, "MSFT": 420, "JNJ": 160, "JPM": 300, "PG": 165}


def setup_world(cfg):
    conn = db.get_conn(cfg)
    for i, (t, p0) in enumerate(START.items()):
        full = walk(300, p0, 0.009, seed_=i + 3)
        put_raw(conn, t, full * 0.97, source="stooq")
        put_raw(conn, t, full[-100:])
    write_tx([(D[250], "SETOR", "CASH", 400, 1, 0, 16200), (D[250], "BELI", "VOO", 0.4, 600, 0.5, 16200),
              (D[260], "BELI", "AAPL", 0.15, 230, 0.2, 16300), (D[280], "BELI", "MSFT", 0.05, 420, 0.2, 16400)])
    conn.execute("INSERT INTO fx_rates VALUES (date('now'), 'USD', 'IDR', 18200, 'frankfurter')")
    conn.execute("INSERT INTO filings VALUES ('acc1','AAPL','8-K',date('now','-3 days'),'http://sec/x','8.01')")
    conn.execute("INSERT INTO earnings VALUES ('MSFT', date('now','localtime','+3 days'), '3.1')")
    conn.commit()
    return conn


def test_pipeline_survives_without_keys_or_network(cfg):
    conn = setup_world(cfg)
    rep = run_daily.pipeline(conn, cfg)
    assert rep["prices"]["status"] == "error" and "ALPHAVANTAGE_API_KEY" in rep["prices"]["msg"]
    assert rep["macro"]["status"] == "skipped" and rep["sec"]["status"] == "error"
    ind, met = run_daily.usable(rep)
    assert ind is not None and set(ind) == set(START)
    assert all(v.get("sma200") is not None for v in ind.values())            # backfill membuat SMA200 tersedia
    assert all(v["dd_window_days"] == 252 for v in ind.values())
    assert met["portfolio_drawdown"]["source"].startswith("nyata") and met["summary"]["usd_idr_now"] == 18200
    probs = daily_job.problems(rep, conn)
    assert any(p.startswith("HARGA") for p in probs) and not any("SPLIT" in p for p in probs)
    assert validate.active_issues(conn) == []

    trig = triggers.collect(conn, ind, met)
    assert ("INFO", "MSFT") in [(l, t) for l, t, x in trig if x.startswith("EARNINGS_")]
    for kind in ("mingguan", "darurat", "kuartalan"):
        path, tokens = report.build(kind, conn, cfg, ind, met, trig)
        text = path.read_text(encoding="utf-8")
        assert tokens <= 3000 and "## Kualitas data" in text and "Sumber posisi: transaksi.csv" in text
        assert "Frankfurter" in text and "[FAKTA-RESMI] 8-K" in text
    conn.close()


def run_job(*args):
    env = dict(os.environ, INVEST_ROOT=str(TMP), INVEST_OFFLINE="1")
    return subprocess.run([sys.executable, str(PROJECT / "src" / "daily_job.py"), *args], capture_output=True, text=True, env=env)


def test_daily_job_dry_run_makes_weekly_report(cfg):
    setup_world(cfg).close()
    r = run_job("--force", "--dry", "--weekly", "--no-news")
    assert r.returncode == 0, r.stderr[-800:]
    assert "laporan mingguan" in r.stderr + r.stdout
    assert list((TMP / "reports" / "mingguan").glob("mingguan_*.md"))
    assert not (TMP / "data" / "job_state.json").exists()                    # --dry tidak menyimpan status


def test_daily_job_refuses_broken_config(cfg):
    setup_world(cfg).close()
    good = (TMP / "config.yaml").read_text(encoding="utf-8")
    try:
        bad = yaml.safe_load(good)
        bad["portfolio"]["satellite"][0]["target_weight"] = 0.5
        (TMP / "config.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
        r = run_job("--force", "--dry")
        assert r.returncode == 0 and "config.yaml bermasalah" in r.stderr + r.stdout
        assert "[harga]" not in r.stdout                                      # pipeline tidak dijalankan
    finally:
        (TMP / "config.yaml").write_text(good, encoding="utf-8")


def test_main_dispatcher():
    env = dict(os.environ, INVEST_ROOT=str(TMP), INVEST_OFFLINE="1")
    r = subprocess.run([sys.executable, str(PROJECT / "main.py")], capture_output=True, text=True, env=env)
    assert "python main.py harian" in r.stdout
    r = subprocess.run([sys.executable, str(PROJECT / "main.py"), "skema", "check"], capture_output=True, text=True, env=env)
    assert "Data contract: OK" in r.stdout, r.stderr[-500:]
