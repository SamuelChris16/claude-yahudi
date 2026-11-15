import os

import pytest
import requests

import backfill
import fetch_analyst
import fetch_earnings
import fetch_fx
import fetch_macro
import http_client
import notify


# ---------- sensor rahasia ----------
def test_secrets_never_survive_redaction(monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "ZXCVBNM123456789")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAExampleTokenValue_x-y")
    raw = ("HTTPSConnectionPool(host='www.alphavantage.co'): Max retries exceeded with url: "
           "/query?function=TIME_SERIES_DAILY&symbol=AAPL&apikey=ZXCVBNM123456789 | "
           "https://finnhub.io/api/v1/company-news?symbol=AAPL&token=abcdef123456 | "
           "https://api.telegram.org/bot123456789:AAExampleTokenValue_x-y/sendMessage | api_key=fredkey999")
    out = http_client.redact(raw)
    for secret in ("ZXCVBNM123456789", "abcdef123456", "AAExampleTokenValue", "fredkey999"):
        assert secret not in out
    assert "symbol=AAPL" in out


def test_network_failure_message_is_redacted(monkeypatch):
    monkeypatch.delenv("INVEST_OFFLINE")

    def boom(method, url, **kw):
        raise requests.ConnectionError("failed: %s?apikey=%s" % (url, kw["params"]["apikey"]))
    monkeypatch.setattr(requests, "request", boom)
    with pytest.raises(http_client.HttpError) as e:
        http_client.get("https://www.alphavantage.co/query", params={"apikey": "SUPERSECRETKEY1"}, retries=0)
    assert "SUPERSECRETKEY1" not in str(e.value) and "alphavantage.co/query" in str(e.value)


def test_retry_on_server_error_then_success(monkeypatch):
    monkeypatch.delenv("INVEST_OFFLINE")
    calls = []

    class R:
        def __init__(self, code):
            self.status_code = code

    def fake(method, url, **kw):
        calls.append(1)
        return R(503 if len(calls) < 3 else 200)
    monkeypatch.setattr(requests, "request", fake)
    assert http_client.get("https://x.test/a", retries=2, backoff=0).status_code == 200 and len(calls) == 3
    monkeypatch.setattr(requests, "request", lambda m, u, **kw: R(404))
    with pytest.raises(http_client.HttpError, match="HTTP 404"):
        http_client.get("https://x.test/a", retries=2, backoff=0)


# ---------- parser sumber ----------
def test_fx_parser_and_freshness(conn):
    v2 = [{"date": "2026-11-12", "base": "USD", "quote": "IDR", "rate": 18155}, {"date": "2026-11-13", "base": "USD", "quote": "IDR", "rate": 18224},
          {"date": "2026-11-13", "base": "USD", "quote": "EUR", "rate": 0.9}, {"date": "2026-11-14", "base": "USD", "quote": "IDR", "rate": None}]
    assert fetch_fx.parse(v2) == [("2026-11-12", 18155.0), ("2026-11-13", 18224.0)]
    assert fetch_fx.parse({"rates": {"2026-11-13": {"IDR": 18224}}}) == [("2026-11-13", 18224.0)]
    assert fetch_fx.parse({"message": "not found"}) == []
    assert fetch_fx.latest(conn) is None
    conn.execute("INSERT INTO fx_rates VALUES ('2020-01-01','USD','IDR',14000,'frankfurter')")
    assert fetch_fx.latest(conn) is None                                    # terlalu tua -> tidak dipakai
    conn.execute("INSERT INTO fx_rates VALUES (date('now','-2 days'),'USD','IDR',18200,'frankfurter')")
    assert fetch_fx.latest(conn)[0] == 18200
    assert fetch_fx.run(conn, {})["status"] == "error"                      # offline: gagal dengan rapi


def test_macro_parser_skips_missing_values(conn, cfg):
    payload = {"observations": [{"date": "2026-11-11", "value": "."}, {"date": "2026-11-12", "value": "17.25"}, {"date": "x"}]}
    assert fetch_macro.parse(payload) == [("2026-11-12", 17.25)]
    assert fetch_macro.run(conn, cfg)["status"] == "skipped"                # tanpa key: dilewati, bukan error


def test_stooq_parser_formats_and_errors():
    a = backfill.parse_csv("Date,Open,High,Low,Close,Volume\n2026-11-12,1,2,0.5,1.5,100\n2026-11-13,1,2,0.5,1.6,120\n")
    assert list(a.date) == ["2026-11-12", "2026-11-13"] and a.close.iloc[-1] == 1.6
    b = backfill.parse_csv("<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\nAAPL.US,D,20261113,000000,1,2,0.5,1.6,120,0\n")
    assert list(b.date) == ["2026-11-13"] and b.volume.iloc[0] == 120
    for text, msg in (("<!DOCTYPE html><html>get your apikey</html>", "API key"), ("Exceeded the daily hits limit", "kuota"),
                      ("No data", "tidak punya data"), ("", "kosong"), ("a,b\n1,2\n", "tidak dikenali")):
        with pytest.raises(ValueError, match=msg):
            backfill.parse_csv(text)
    assert backfill.stooq_symbol("BRK.B") == "brk-b.us"


def test_backfill_without_key_explains_what_to_do(conn, cfg):
    assert "STOOQ_API_KEY" in backfill.run(conn, cfg)["msg"]


def test_backfill_from_folder(conn, cfg, tmp_path):
    rows = "\n".join("%s,10,11,9,10,1000" % d for d in ("2026-11-10", "2026-11-11", "2026-11-12"))
    (tmp_path / "voo_us_d.csv").write_text("Date,Open,High,Low,Close,Volume\n" + rows)
    res = backfill.run(conn, cfg, ["VOO", "AAPL"], folder=str(tmp_path))
    assert res["filled"] == ["VOO (3 baris)"] and "AAPL" in res["failed"][0]
    assert conn.execute("SELECT COUNT(*) FROM prices_raw WHERE source='stooq'").fetchone()[0] == 3


def test_earnings_and_analyst_parsers():
    text = "symbol,name,reportDate,fiscalDateEnding,estimate,currency\nMSFT,Microsoft,2026-11-18,2026-09-30,3.1,USD\nXYZ,X,2026-11-19,2026-09-30,,USD\n"
    assert fetch_earnings.parse(text, {"MSFT"}) == [("MSFT", "2026-11-18", "3.1")]
    assert fetch_earnings.parse('{"Information": "limit"}', {"MSFT"}) is None
    assert fetch_analyst.parse([{"period": "2026-11-01", "strongBuy": 10, "buy": 20, "hold": 5, "sell": 1, "strongSell": 0}, {"bad": 1}]) == [
        ("2026-11-01", 10, 20, 5, 1, 0)]
    assert fetch_analyst.parse({"error": "no access"}) == []


# ---------- notifikasi ----------
def test_trigger_magnitude_and_worsening_resend():
    assert notify._num("TURUN_5H_-12.3%") == -12.3
    assert notify._num("PORTOFOLIO_DRAWDOWN -10.29%") == -10.29
    assert notify._num("BERITA_MATERIAL (2 media): Apple 5 things") is None
    key = "MSFT|" + notify._norm("TURUN_5H_-12.3%")
    assert key == "MSFT|" + notify._norm("TURUN_5H_-25.0%")                 # pemicu yang sama, angka berbeda
    assert notify.should_send(key, -12.3)
    notify.mark(key, -12.3)
    assert not notify.should_send(key, -13.0)                               # belum memburuk 3 poin
    assert notify.should_send(key, -16.0)
    assert notify.send("t", "m", {"notify": {"channels": ["telegram"]}}) == {"telegram": "skip (token/chat id kosong)"}
