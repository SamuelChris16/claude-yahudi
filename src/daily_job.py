"""Job terjadwal harian (dipanggil launchd): data -> pemeriksaan -> laporan -> notifikasi -> sinkronisasi -> backup.

Pakai:  python src/daily_job.py              normal (hanya Selasa-Sabtu, sekali sukses per hari)
        python src/daily_job.py --force      abaikan pembatas hari/"sudah sukses hari ini"
        python src/daily_job.py --dry        tanpa notifikasi, tanpa sinkronisasi, tanpa simpan status
        python src/daily_job.py --weekly     paksa buat laporan mingguan
        python src/daily_job.py --quarterly  paksa buat laporan kuartalan
        python src/daily_job.py --no-news    lewati berita

config.yaml (semua opsional):
  schedule: {weekly_report_weekday: 5}   # 0=Senin ... 5=Sabtu (default Sabtu)
  sync:     {drive_dir: "~/Library/CloudStorage/GoogleDrive-EMAIL/My Drive/invest-advisor"}
  notify:   {channels: [macos, telegram], cooldown_days: 3}
"""
import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np

import notify
import report as rpt
import run_daily
import triggers
from db import ROOT, get_conn, load_config

STATE = ROOT / "data" / "job_state.json"
LOCK = ROOT / "data" / "job.lock"


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    log = logging.getLogger("job")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        for h in (RotatingFileHandler(ROOT / "logs" / "daily.log", maxBytes=500_000, backupCount=3, encoding="utf-8"),
                  logging.StreamHandler()):
            h.setFormatter(fmt)
            log.addHandler(h)
    return log


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def acquire():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            os.kill(int(LOCK.read_text()), 0)
            return False  # proses lain masih berjalan
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        except ValueError:
            pass
    LOCK.write_text(str(os.getpid()))
    return True


def release():
    LOCK.unlink(missing_ok=True)


def problems(rep):
    out = []
    for key, label in (("prices", "HARGA"), ("news", "BERITA"), ("sec", "SEC"), ("earnings", "EARNINGS")):
        r = rep.get(key) or {}
        if r.get("status") == "error":
            out.append(f"{label}: {str(r.get('msg'))[:140]}")
    p = rep.get("prices") or {}
    if p.get("failed"):
        out.append("HARGA: " + "; ".join(p["failed"])[:200])
    if (rep.get("indicators") or {}).get("status") == "error":
        out.append("INDIKATOR: gagal dihitung")
    if (rep.get("metrics") or {}).get("status") == "error":
        out.append("METRIK: gagal dihitung")
    return out


def sync_drive(paths, cfg, log):
    d = (cfg.get("sync") or {}).get("drive_dir")
    if not d:
        return
    base = Path(d).expanduser()
    for p in paths:
        dest = base / p.parent.name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / p.name)
        log.info("disalin ke Drive: %s", dest / p.name)


def backup(conn, today, keep=8):
    d = ROOT / "backups"
    d.mkdir(exist_ok=True)
    dest = d / f"market_{today}.db"
    if not dest.exists():
        b = sqlite3.connect(dest)
        conn.backup(b)
        b.close()
    for old in sorted(d.glob("market_*.db"))[:-keep]:
        old.unlink()
    return dest


def run(a, log):
    cfg = load_config()
    conn = get_conn(cfg)
    st = load_state()
    today = date.today()
    rep = run_daily.pipeline(conn, cfg, news=not a.no_news)
    probs = problems(rep)

    # ---- kesegaran data & hari libur bursa ----
    last = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    new_data = last != st.get("last_price_date")
    st["unchanged_runs"] = 0 if new_data else st.get("unchanged_runs", 0) + 1
    if last:
        age = int(np.busday_count(last, today.isoformat()))
        if age > 3 or st["unchanged_runs"] >= 3:
            probs.append(f"DATA_BASI: harga terakhir {last} ({age} hari kerja lalu), cek key/kuota/jaringan")
    else:
        probs.append("DATA_BASI: belum ada data harga sama sekali")
    st["last_price_date"] = last
    log.info("harga terakhir=%s data_baru=%s kuota_AV=%s", last, new_data, rep["quota_alphavantage_used_today"])

    ind, met = rep["indicators"], rep["metrics"]
    made = []
    if isinstance(ind, dict) and "summary" in met:
        trig = triggers.collect(conn, ind, met)
        if triggers.has_emergency(trig):
            path, tok = rpt.build("darurat", conn, cfg, ind, met, trig)
            made.append(path)
            log.warning("PEMICU DARURAT -> %s (~%s token)", path, tok)
            if not a.dry:
                log.info("notifikasi darurat: %s", notify.notify_triggers(trig, path, cfg))

        wd = (cfg.get("schedule") or {}).get("weekly_report_weekday", 5)
        if a.weekly or today.weekday() == wd:
            path, tok = rpt.build("mingguan", conn, cfg, ind, met, trig)
            made.append(path)
            log.info("laporan mingguan: %s (~%s token)", path, tok)
            if not a.dry:
                notify.send("Laporan mingguan siap", f"{path.name}. Unggah ke Drive lalu minta Claude menganalisis.", cfg)

        q = f"{today.year}Q{(today.month - 1) // 3 + 1}"
        if a.quarterly or (today.month in (3, 6, 9, 12) and today.day >= 22 and st.get("quarterly_done") != q):
            path, tok = rpt.build("kuartalan", conn, cfg, ind, met, trig)
            made.append(path)
            log.info("laporan kuartalan: %s (~%s token)", path, tok)
            if not a.dry:
                st["quarterly_done"] = q
                notify.send("Laporan kuartalan siap", f"{path.name}. Saatnya evaluasi relokasi bersama Claude.", cfg)
        if today.weekday() == 5 and not a.dry:
            log.info("backup: %s", backup(conn, today))

    if not a.dry:
        sync_drive(made, cfg, log)
        if probs:
            log.error("MASALAH: %s", probs)
            notify.notify_failure(probs, cfg)
        if not any(p.startswith("HARGA") for p in probs):
            st["last_success"] = today.isoformat()
        save_state(st)
    else:
        log.info("[dry] masalah=%s laporan=%s", probs, [str(p) for p in made])


def main():
    ap = argparse.ArgumentParser()
    for f in ("force", "dry", "weekly", "quarterly", "no_news"):
        ap.add_argument("--" + f.replace("_", "-"), action="store_true")
    a = ap.parse_args()
    log = setup_logging()
    today = date.today()
    if not a.force and today.weekday() not in (1, 2, 3, 4, 5):
        log.info("Senin/Minggu: belum ada data bursa baru, dilewati")
        return
    if not a.force and not a.dry and load_state().get("last_success") == today.isoformat():
        log.info("sudah sukses hari ini, dilewati")
        return
    if not acquire():
        log.warning("job lain masih berjalan, dilewati")
        return
    try:
        run(a, log)
    except Exception as e:  # noqa: BLE001
        log.exception("job gagal")
        if not a.dry:
            notify.notify_failure([f"JOB: {type(e).__name__}: {e}"], load_config())
    finally:
        release()


if __name__ == "__main__":
    main()
