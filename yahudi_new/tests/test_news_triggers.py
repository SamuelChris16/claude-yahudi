import json
from datetime import datetime, timedelta, timezone

import fetch_news
import triggers
from conftest import TMP

WL = ["Reuters", "Bloomberg", "Wall Street Journal", "CNBC", "Barron's", "Financial Times", "MarketWatch", "Associated Press"]


def test_whitelist_accepts_aliases_and_rejects_lookalikes():
    for ok in ("CNBC", "Reuters", "WSJ", "The Wall Street Journal", "AP", "AP News", "Barrons", "Bloomberg.com", "FT"):
        assert fetch_news.source_allowed(ok, WL), ok
    for no in ("Yahoo", "SeekingAlpha", "Benzinga", "Snapchat", "Motley Fool", "", None, "Crafty Reutersish Blog", "APK Mirror"):
        assert not fetch_news.source_allowed(no, WL), no


def test_company_name_cleaning():
    assert fetch_news.clean_company_name("Apple Inc.") == "apple"
    assert fetch_news.clean_company_name("MICROSOFT CORP") == "microsoft"
    assert fetch_news.clean_company_name("JPMORGAN CHASE & CO") == "jpmorgan chase"
    assert fetch_news.clean_company_name("PROCTER & GAMBLE Co") == "procter & gamble"
    assert fetch_news.clean_company_name("JOHNSON & JOHNSON") == "johnson & johnson"


def item(ticker, source, title, summary="", url=None, hours_ago=5, relevance=None):
    pub = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None).isoformat(timespec="seconds")
    return dict(ticker=ticker, source=source, title=title, summary=summary, url=url or "http://x/%s" % abs(hash(title + source)),
                published=pub, sentiment=None, relevance=relevance)


TERMS = {"JNJ": ["johnson & johnson", "johnson"], "JPM": ["jpmorgan chase", "jpmorgan"], "AAPL": ["apple"], "MSFT": ["microsoft"]}


def test_irrelevant_and_non_whitelisted_news_are_dropped(cfg):
    items = [item("JNJ", "CNBC", "A little-known biotech's cancer bet just paid off", "Genmab shares have doubled"),
             item("JPM", "CNBC", "Outside groups urge congressional Dems to target corporate interests"),
             item("JNJ", "CNBC", "Johnson & Johnson raises full-year outlook"),
             item("JPM", "Reuters", "Dimon says JPM will keep hiring"),
             item("AAPL", "Yahoo", "Apple hits record"),
             item("AAPL", "CNBC", "Tech rally broadens", relevance=0.6)]           # Alpha Vantage: relevansi dari API
    final, stats = fetch_news.process(items, cfg, TERMS)
    assert sorted(c["title"][:12] for c in final) == ["Dimon says J", "Johnson & Jo", "Tech rally b"]
    assert stats == dict(raw=6, not_whitelisted=1, not_relevant=2)


def test_material_keywords_match_whole_phrases_in_title_only(cfg):
    titles = {"Apple changes default search engine deal": 0, "Analyst recalls 2008 as Apple rallies": 0,
              "Apple rally shows no sign of halting": 0, "Apple faces SEC investigation over disclosures": 1,
              "Apple supplier files for bankruptcy": 1, "Apple announces product recall for chargers": 1}
    final, _ = fetch_news.process([item("AAPL", "Reuters", t) for t in titles], cfg | {"news": {**cfg["news"], "max_items_per_ticker": 20}}, TERMS)
    assert {c["title"]: c["material"] for c in final} == titles
    final, _ = fetch_news.process([item("AAPL", "Reuters", "Apple outlook steady", "unrelated fraud case elsewhere")], cfg, TERMS)
    assert final[0]["material"] == 0                                                # ringkasan tidak dihitung


def test_dedup_counts_independent_outlets_and_tags_rumor(cfg):
    items = [item("MSFT", "Reuters", "Microsoft in talks to buy startup, sources say", url="http://a"),
             item("MSFT", "CNBC", "Microsoft in talks to buy startup - sources say", url="http://b"),
             item("MSFT", "Reuters", "Microsoft in talks to buy startup, sources say", url="http://a")]
    final, _ = fetch_news.process(items, cfg, TERMS)
    assert len(final) == 1 and final[0]["n_outlets"] == 2 and final[0]["tier"] == "RUMOR-TERBATAS"


def test_company_terms_from_sec_list_and_config(cfg):
    (TMP / "data").mkdir(exist_ok=True)
    (TMP / "data" / "company_tickers.json").write_text(json.dumps({
        "0": {"cik_str": 1, "ticker": "JPM", "title": "JPMORGAN CHASE & CO"}, "1": {"cik_str": 2, "ticker": "AAPL", "title": "Apple Inc."}}))
    cfg["portfolio"]["satellite"][0]["names"] = ["iPhone maker"]
    terms = fetch_news.company_terms(cfg)
    assert terms["JPM"] == ["jpmorgan", "jpmorgan chase"] and terms["AAPL"] == ["apple", "iphone maker"]
    assert terms["PG"] == []                                                        # tidak dikenal -> tidak disaring
    assert fetch_news.is_relevant("PG", "Consumer giant lifts prices", terms) is None
    assert fetch_news.is_relevant("PG", "PG lifts prices", terms) is True
    assert fetch_news.is_relevant("JPM", "Chase bank fees explained", terms) is False


def put_news(conn, ticker, title, n_outlets, hours_ago=2):
    pub = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute("INSERT INTO news (id, ticker, source, title, summary, url, published, tier, n_outlets, material) "
                 "VALUES (?,?,?,?,?,?,?,?,?,1)", (title, ticker, "Reuters", title, "", "http://x", pub, "BERITA", n_outlets))


def test_material_news_needs_two_outlets_for_emergency(conn):
    put_news(conn, "AAPL", "Apple faces SEC investigation", 1)
    put_news(conn, "MSFT", "Microsoft files restatement", 3)
    put_news(conn, "JNJ", "Old bankruptcy story", 4, hours_ago=24 * 5)
    trig = triggers.collect(conn, {}, {})
    assert [(l, t) for l, t, _ in trig] == [("DARURAT", "MSFT"), ("PERINGATAN", "AAPL")]
    assert triggers.has_emergency(trig)


def test_indicator_and_data_triggers(conn):
    ind = {"AAPL": {"emergency": [], "flags": ["SPLIT_PERLU_KONFIRMASI", "RSI_OVERBOUGHT"]},
           "MSFT": {"emergency": ["TURUN_5H_-14.0%"], "flags": []}, "JNJ": {"error": "data kurang"}}
    conn.execute("INSERT INTO data_issues (ticker, date, kind, severity, detail) VALUES ('PG','','rekonsiliasi_gagal','PERINGATAN','beda 9%')")
    met = {"emergency": [], "alerts": ["REBALANCE VOO: +29.9 pp dari target (band ±5)", "BOBOT_SEKTOR Technology: 35.0% > batas 30%"]}
    trig = triggers.collect(conn, ind, met)
    assert trig[0] == ("DARURAT", "MSFT", "TURUN_5H_-14.0%")
    assert ("PERINGATAN", "AAPL", "SPLIT_PERLU_KONFIRMASI") in trig and ("INFO", "AAPL", "RSI_OVERBOUGHT") in trig
    assert any(l == "PERINGATAN" and t == "PG" and "REKONSILIASI_GAGAL" in x for l, t, x in trig)
    assert ("INFO", "VOO", "REBALANCE VOO: +29.9 pp dari target (band ±5)") in trig
    assert any(l == "PERINGATAN" and t == "PORTOFOLIO" for l, t, _ in trig)


def test_emergencies_for_stocks_not_owned_are_downgraded(conn):
    ind = {"JPM": {"emergency": ["DRAWDOWN_-22.7%"], "flags": []}, "VOO": {"emergency": ["TURUN_5H_-13.0%"], "flags": []}}
    met = {"positions": [dict(ticker="VOO", shares=0.4), dict(ticker="JPM", shares=0.0)]}
    put_news(conn, "JPM", "JPMorgan faces SEC investigation", 3)
    trig = triggers.collect(conn, ind, met)
    assert trig[0] == ("DARURAT", "VOO", "TURUN_5H_-13.0%")
    assert ("INFO", "JPM", "DRAWDOWN_-22.7% (belum dimiliki)") in trig
    assert [l for l, t, x in trig if t == "JPM" and x.startswith("BERITA_MATERIAL")] == ["PERINGATAN"]
