"""Gabungkan semua pemicu jadi satu daftar (level DARURAT / PERINGATAN / INFO)."""
from datetime import datetime, timedelta, timezone

import validate

ORDER = {"DARURAT": 0, "PERINGATAN": 1, "INFO": 2}


def _alert(a):
    kind, _, rest = a.partition(" ")
    subject = rest.split(":")[0].split(" ")[0] if kind in ("REBALANCE", "BOBOT_SAHAM") else "PORTOFOLIO"
    level = "PERINGATAN" if kind.startswith("BOBOT") or kind.startswith("DRAWDOWN") else "INFO"
    return level, subject, a


def collect(conn, ind, met):
    out = []
    pos = met.get("positions")
    held = {p["ticker"] for p in pos if p.get("shares", 0) > 0} if pos else None   # None = posisi tidak diketahui

    def owned(t):
        return held is None or t in held

    for t, v in (ind or {}).items():
        if not isinstance(v, dict):
            continue
        for e in v.get("emergency", []):
            # saham pantauan yang belum dimiliki tidak bisa merugikan portofolio: cukup INFO
            out.append(("DARURAT", t, e) if owned(t) else ("INFO", t, e + " (belum dimiliki)"))
        for f in v.get("flags", []):
            out.append(("PERINGATAN" if f == "SPLIT_PERLU_KONFIRMASI" else "INFO", t, f))
    for e in met.get("emergency", []):
        out.append(("DARURAT", "PORTOFOLIO", e))
    for a in met.get("alerts", []):
        out.append(_alert(a))

    # berita material 3 hari terakhir (waktu terbit tersimpan dalam UTC).
    # DARURAT hanya bila dilaporkan >= 2 media independen; satu media saja -> PERINGATAN.
    since = (datetime.now(timezone.utc) - timedelta(days=3)).replace(tzinfo=None).isoformat(timespec="seconds")
    for t, title, n in conn.execute(
            "SELECT ticker, title, n_outlets FROM news WHERE material=1 AND published>=? ORDER BY published DESC", (since,)):
        out.append(("DARURAT" if (n or 1) >= 2 and owned(t) else "PERINGATAN", t,
                    "BERITA_MATERIAL (%d media): %s" % (n or 1, title[:100])))

    for t, d in conn.execute("SELECT ticker, report_date FROM earnings WHERE report_date BETWEEN date('now','localtime') "
                             "AND date('now','localtime','+7 days') ORDER BY report_date"):
        out.append(("INFO", t, "EARNINGS_%s" % d))

    # kualitas data: masalah harga yang membuat angka tidak bisa dipercaya
    for t, d, kind, _sev, detail in validate.active_issues(conn, ("PERINGATAN",)):
        if kind != "split_perlu_konfirmasi":       # yang ini sudah muncul lewat flag indikator
            out.append(("PERINGATAN", t, "DATA_%s %s: %s" % (kind.upper(), d or "", detail[:90])))

    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return sorted(uniq, key=lambda x: ORDER[x[0]])


def has_emergency(trig):
    return any(l == "DARURAT" for l, _, _ in trig)
