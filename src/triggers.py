"""Fase 3: gabungkan semua pemicu jadi satu daftar (level DARURAT / PERINGATAN / INFO)."""
from datetime import datetime, timedelta


def collect(conn, ind, met):
    out = []
    for t, v in ind.items():
        for e in v.get("emergency", []):
            out.append(("DARURAT", t, e))
        for f in v.get("flags", []):
            out.append(("INFO", t, f))
    for e in met.get("emergency", []):
        out.append(("DARURAT", "PORTOFOLIO", e))
    for a in met.get("alerts", []):
        kind, _, rest = a.partition(" ")
        tick = rest.split(":")[0].split(" ")[0] if kind in ("REBALANCE", "BOBOT_SAHAM") else "PORTOFOLIO"
        out.append(("PERINGATAN" if kind.startswith("BOBOT") else "INFO", tick, a))

    since = (datetime.now() - timedelta(days=3)).isoformat()
    for t, title in conn.execute(
            "SELECT ticker, title FROM news WHERE material=1 AND published>=? ORDER BY published DESC", (since,)):
        out.append(("DARURAT", t, f"BERITA_MATERIAL: {title[:100]}"))
    for t, d in conn.execute("SELECT ticker, report_date FROM earnings WHERE report_date BETWEEN date('now') "
                             "AND date('now','+7 days') ORDER BY report_date"):
        out.append(("INFO", t, f"EARNINGS_{d}"))
    # buang duplikat, urutkan: DARURAT dulu
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    order = {"DARURAT": 0, "PERINGATAN": 1, "INFO": 2}
    return sorted(uniq, key=lambda x: order[x[0]])


def has_emergency(trig):
    return any(l == "DARURAT" for l, _, _ in trig)
