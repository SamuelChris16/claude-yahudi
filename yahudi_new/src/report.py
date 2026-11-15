"""Fase 4: penyusun laporan markdown untuk Claude.

Pakai:  python src/report.py mingguan
        python src/report.py darurat [--force]
        python src/report.py kuartalan
Hasil:  reports/<jenis>/<jenis>_YYYY-MM-DD.md  (+ perkiraan jumlah token)
"""
import sys
from datetime import date

import indicators
import metrics
import triggers
import validate
from db import ROOT, get_conn, load_config

LEGENDA = ("Legenda: FAKTA-RESMI = filing SEC; BERITA = media whitelist; RUMOR-TERBATAS = media kredibel dengan sumber anonim, "
           "belum terkonfirmasi; DATA = hasil hitung lokal; n_outlet = jumlah media independen yang melaporkan.")


def table(headers, rows):
    if not rows:
        return "_(kosong)_\n"
    s = "| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n"
    return s + "\n".join("| " + " | ".join("n/a" if c is None else str(c) for c in r) + " |" for r in rows) + "\n"


def pct(a, b):
    return None if not a or not b else round((a / b - 1) * 100, 1)


def _sgn(x, suffix=""):
    return None if x is None else "%+g%s" % (x, suffix)


def sec_positions(met):
    rows = [(p["ticker"], p["sector"], p["value"], None if p["weight_pct"] is None else "%s%%" % p["weight_pct"],
             "%s%%" % p["target_pct"], _sgn(p["dev_pp"]), _sgn(p["pnl_pct"], "%")) for p in met.get("positions", [])]
    s = met.get("summary", {})
    head = "Nilai saham $%s | kas $%s | P&L belum terealisasi $%s (%s%%)" % (
        s.get("invested_usd"), s.get("cash_usd"), s.get("pnl_usd"), s.get("pnl_pct"))
    if "realized_usd" in s:
        head += " | terealisasi $%s | dividen $%s" % (s["realized_usd"], s["dividends_usd"])
    if "value_idr" in s:
        head += ("\n\nNilai total Rp{:,} (kurs {}; sumber: {}). Untung/rugi Rp{:,} = dari harga saham Rp{:,} + dari kurs Rp{:,}."
                 .format(s["value_idr"], s["usd_idr_now"], s.get("fx_source"), s["pnl_idr_total"],
                         s["pnl_idr_from_stock"], s["pnl_idr_from_fx"]))
    head += "\n\nSumber posisi: %s." % met.get("position_source", "?")
    return head + "\n\n" + table(["Ticker", "Sektor", "Nilai $", "Bobot", "Target", "Selisih pp", "P&L"], rows)


def sec_technical(ind, detail=False):
    rows = []
    for t, v in ind.items():
        if "error" in v:
            rows.append((t, v["error"]) + (None,) * 7)
            continue
        r = [t, v["close"], v["rsi"], pct(v["close"], v.get("sma50")), pct(v["close"], v.get("sma200")),
             v["ret_1w"], v["ret_1m"], v["drawdown_52w_pct"], ",".join(v["flags"] + v["emergency"]) or "-"]
        if detail:
            r[8:8] = [v["ret_3m"], v["macd_hist"], v["atr_pct"], v["vol_annual_pct"]]
        rows.append(r)
    h = ["Ticker", "Harga", "RSI", "vs SMA50 %", "vs SMA200 %", "1W %", "1M %", "DD puncak %"]
    if detail:
        h += ["3M %", "MACD hist", "ATR %", "Vol thn %"]
    short = sorted({v["dd_window_days"] for v in ind.values() if isinstance(v, dict) and v.get("dd_window_days", 252) < 252})
    note = "_SMA200 'n/a' = riwayat harga belum 200 hari._\n"
    if short:
        note += "_DD puncak dihitung dari riwayat %s hari bursa (belum setahun penuh)._\n" % "/".join(map(str, short))
    return table(h + ["Flag"], rows) + note


def sec_news(conn, tickers, per):
    if not tickers:
        return "_(tidak ada saham relevan)_\n"
    out = []
    for t in tickers:
        rows = conn.execute("SELECT tier, source, published, title, summary, n_outlets, material, url FROM news "
                            "WHERE ticker=? ORDER BY material DESC, published DESC LIMIT ?", (t, per)).fetchall()
        for tier, src, pub, title, summ, n, mat, url in rows:
            out.append(f"- **{t}** [{tier}{' ·MATERIAL' if mat else ''}] {pub[:10]} {src} (n_outlet={n}): {title}"
                       + (f" — {summ[:160]}" if summ else "") + f" <{url}>")
    return "\n".join(out) + "\n" if out else "_(tidak ada berita lolos whitelist)_\n"


def sec_filings(conn, tickers):
    out = []
    for t in tickers:
        f8 = conn.execute("SELECT filed, items, url FROM filings WHERE ticker=? AND form='8-K' AND filed>=date('now','-14 days') "
                          "ORDER BY filed DESC LIMIT 3", (t,)).fetchall()
        n4 = conn.execute("SELECT COUNT(*) FROM filings WHERE ticker=? AND form='4' AND filed>=date('now','-14 days')", (t,)).fetchone()[0]
        for filed, items, url in f8:
            out.append(f"- **{t}** [FAKTA-RESMI] 8-K {filed} item {items or '?'} <{url}>")
        if n4:
            out.append(f"- **{t}** [FAKTA-RESMI] {n4} Form 4 (transaksi insider) dalam 14 hari — isi belum dianalisis")
    return "\n".join(out) + "\n" if out else "_(tidak ada filing baru)_\n"


def sec_triggers(trig, levels=("DARURAT", "PERINGATAN", "INFO")):
    rows = [(l, t, x) for l, t, x in trig if l in levels]
    return table(["Level", "Subjek", "Pemicu"], rows)


def sec_alloc(met):
    a = met.get("savings_allocation", {})
    rows = [(t, f"${v}") for t, v in a.get("allocation_usd", {}).items()]
    if a.get("unallocated_usd"):
        rows.append(("(tahan sebagai kas)", f"${a['unallocated_usd']}"))
    return (f"Dana siap: ${a.get('deployable_usd')} (tabungan ${a.get('weekly_usd')} + kas ${a.get('cash_usd')}, kurs {a.get('fx_used')}).\n"
            f"Saran mekanis (hanya menutup selisih ke target; belum menimbang berita/teknikal):\n\n" + table(["Ticker", "Beli"], rows))


def sec_rebalance(met):
    inv = met["summary"]["invested_usd"]
    rows = [(p["ticker"], "%s%%" % p["weight_pct"], "%s%%" % p["target_pct"],
             "%+.2f" % ((p["target_pct"] - p["weight_pct"]) / 100 * inv)) for p in met["positions"] if p["weight_pct"] is not None]
    return "Selisih ke bobot target jika direlokasi penuh (+ beli, - jual; sebelum biaya dan pajak):\n\n" + table(
        ["Ticker", "Bobot", "Target", "$"], rows)


def sec_quality(conn):
    rows = [(t, d or "-", k, x) for t, d, k, _s, x in validate.active_issues(conn, ("PERINGATAN",))]
    return table(["Ticker", "Tanggal", "Masalah", "Keterangan"], rows) if rows else "Tidak ada masalah kualitas data yang aktif.\n"


def sec_journal():
    p = ROOT / "jurnal" / "keputusan.md"
    return p.read_text(encoding="utf-8")[-1500:] + "\n" if p.exists() else "_(belum ada jurnal keputusan)_\n"


def sec_warn(met, ind, conn):
    w = list(met.get("warnings", []))
    short = [t for t, v in ind.items() if "error" in v]
    if short:
        w.append("data harga kurang untuk: " + ", ".join(short))
    last = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    w.append(f"data harga terakhir: {last}")
    return "\n".join(f"- {x}" for x in w) + "\n"


def build(kind, conn, cfg, ind=None, met=None, trig=None):
    ind = ind if ind is not None else indicators.run(conn, cfg)
    met = met if met is not None else metrics.run(conn, cfg)
    trig = trig if trig is not None else triggers.collect(conn, ind, met)
    if "summary" not in met:
        raise ValueError("laporan tidak bisa dibuat: " + "; ".join(met.get("warnings", ["tidak ada posisi"]))[:200])
    today = date.today().isoformat()
    hit = sorted({t for l, t, _ in trig if l in ("DARURAT", "PERINGATAN") and t in ind})
    allt = list(ind)
    L = [f"# Laporan {kind.upper()} — {today}", LEGENDA, ""]

    if kind == "darurat":
        port_level = any(t == "PORTOFOLIO" for l, t, _ in trig if l == "DARURAT")
        focus = allt if port_level or not hit else hit
        L += ["## Pemicu aktif", sec_triggers(trig, ("DARURAT", "PERINGATAN")),
              "## Posisi portofolio", sec_positions(met),
              "## Drawdown portofolio", f"{met.get('portfolio_drawdown')}\n",
              "## Teknikal saham terdampak", sec_technical({t: ind[t] for t in focus if t in ind}, True),
              "## Berita", sec_news(conn, focus, 3), "## Filing resmi", sec_filings(conn, focus),
              "## Jurnal keputusan terakhir", sec_journal(), "## Kualitas data", sec_quality(conn), "## Catatan data", sec_warn(met, ind, conn)]
    elif kind == "mingguan":
        L += ["## Ringkasan portofolio", sec_positions(met), "## Alokasi tabungan minggu ini", sec_alloc(met),
              "## Pemicu dan peringatan", sec_triggers(trig), "## Teknikal", sec_technical(ind),
              "## Sektor dan korelasi", f"Sektor (%): {met.get('sector_weights_pct')}\n\nKorelasi tinggi: {met.get('high_correlation') or 'tidak ada'}\n",
              "## Berita", sec_news(conn, allt, 3), "## Filing resmi", sec_filings(conn, [t for t in allt if t not in {x['ticker'] for x in cfg['portfolio']['core']}]),
              "## Kandidat emiten baru", "_(belum tersedia: modul screening belum dibuat)_\n",
              "## Jurnal keputusan terakhir", sec_journal(), "## Kualitas data", sec_quality(conn), "## Catatan data", sec_warn(met, ind, conn)]
    else:  # kuartalan
        L += ["## Ringkasan portofolio", sec_positions(met), "## Relokasi ke target", sec_rebalance(met),
              "## Alokasi tabungan minggu ini", sec_alloc(met),
              "## Risiko", f"Sektor (%): {met.get('sector_weights_pct')}\n\nDrawdown: {met.get('portfolio_drawdown')}\n\n"
              f"Korelasi tinggi: {met.get('high_correlation') or 'tidak ada'}\n\nLikuiditas: {met.get('liquidity')}\n",
              "## Pemicu dan peringatan", sec_triggers(trig), "## Teknikal lengkap", sec_technical(ind, True),
              "## Berita", sec_news(conn, allt, 5), "## Filing resmi", sec_filings(conn, [t for t in allt if t not in {x['ticker'] for x in cfg['portfolio']['core']}]),
              "## Kandidat emiten baru", "_(belum tersedia: modul screening belum dibuat)_\n",
              "## Jurnal keputusan", sec_journal(), "## Kualitas data", sec_quality(conn), "## Catatan data", sec_warn(met, ind, conn)]

    text = "\n".join(L)
    d = ROOT / "reports" / kind
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{kind}_{today}.md"
    path.write_text(text, encoding="utf-8")
    return path, len(text) // 4


if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "mingguan"
    if kind not in ("mingguan", "darurat", "kuartalan"):
        sys.exit("jenis harus: mingguan | darurat | kuartalan")
    cfg = load_config()
    conn = get_conn(cfg)
    ind, met = indicators.run(conn, cfg), metrics.run(conn, cfg)
    trig = triggers.collect(conn, ind, met)
    if kind == "darurat" and not triggers.has_emergency(trig) and "--force" not in sys.argv:
        sys.exit("Tidak ada pemicu DARURAT, laporan tidak dibuat (pakai --force untuk memaksa).")
    try:
        path, tok = build(kind, conn, cfg, ind, met, trig)
    except ValueError as e:
        sys.exit(str(e))
    print(f"Laporan: {path}  (~{tok} token)")
