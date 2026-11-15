"""Kurs USD/IDR harian dari Frankfurter (gratis, tanpa key). Ini kurs referensi, bukan kurs broker."""
from datetime import date, timedelta

import http_client
from db import fetched_today, get_conn, load_config, log_fetch

URL = "https://api.frankfurter.dev/v2/rates"


def parse(payload, base="USD", quote="IDR"):
    """v2: daftar {date, base, quote, rate}. Juga menerima bentuk v1 {rates: {tanggal: {IDR: x}}}."""
    rows = []
    if isinstance(payload, list):
        for x in payload:
            if str(x.get("base", "")).upper() == base and str(x.get("quote", "")).upper() == quote:
                rows.append((str(x.get("date")), x.get("rate")))
    elif isinstance(payload, dict) and isinstance(payload.get("rates"), dict):
        for d, v in payload["rates"].items():
            if isinstance(v, dict) and quote in v:
                rows.append((d, v[quote]))
    return [(d, float(r)) for d, r in rows if isinstance(r, (int, float)) and r > 0 and len(d) == 10]


def run(conn, cfg, base="USD", quote="IDR"):
    if fetched_today(conn, "frankfurter", "rates", quote):
        return {"status": "cached"}
    last = conn.execute("SELECT MAX(date) FROM fx_rates WHERE base=? AND quote=?", (base, quote)).fetchone()[0]
    start = (date.fromisoformat(last) - timedelta(days=5)) if last else (date.today() - timedelta(days=5 * 365))
    try:
        rows = parse(http_client.get(URL, params={"base": base.lower(), "quotes": quote.lower(),
                                                  "from": start.isoformat()}).json(), base, quote)
    except Exception as e:  # noqa: BLE001
        log_fetch(conn, "frankfurter", "rates", quote, "error")
        return {"status": "error", "msg": http_client.redact(e)}
    if not rows:
        log_fetch(conn, "frankfurter", "rates", quote, "empty")
        return {"status": "error", "msg": "Frankfurter tidak mengembalikan kurs %s/%s" % (base, quote)}
    conn.executemany("INSERT OR REPLACE INTO fx_rates VALUES (?,?,?,?,'frankfurter')", [(d, base, quote, r) for d, r in rows])
    conn.commit()
    log_fetch(conn, "frankfurter", "rates", quote, "ok")
    d, r = max(rows)
    return {"status": "ok", "rows": len(rows), "latest": "%s = %s" % (d, r)}


def latest(conn, base="USD", quote="IDR", max_age_days=10):
    """(kurs, tanggal) terbaru, atau None bila tidak ada / lebih tua dari max_age_days."""
    r = conn.execute("SELECT rate, date FROM fx_rates WHERE base=? AND quote=? ORDER BY date DESC LIMIT 1", (base, quote)).fetchone()
    if not r or (date.today() - date.fromisoformat(r[1])).days > max_age_days:
        return None
    return r[0], r[1]


if __name__ == "__main__":
    cfg = load_config()
    conn = get_conn(cfg)
    print(run(conn, cfg), latest(conn))
