"""Notifikasi (macOS + Telegram), anti-spam, dan mode jeda.

CLI:  python src/notify.py test      kirim notifikasi uji (abaikan mode jeda)
      python src/notify.py pause     matikan notifikasi (job tetap jalan)
      python src/notify.py resume    nyalakan lagi
.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (opsional)
config.yaml (opsional): notify: {channels: [macos, telegram], cooldown_days: 3}
"""
import json
import os
import platform
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests

from db import ROOT

STATE = ROOT / "data" / "notify_state.json"
PAUSE = ROOT / "data" / "PAUSE"
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?%?")


def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def is_paused():
    return PAUSE.exists()


def _macos(title, msg):
    if platform.system() != "Darwin":
        return "skip (bukan macOS)"
    esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(msg)[:240]}" with title "{esc(title)}"'],
                       check=True, timeout=10, capture_output=True)
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}"


def _telegram(title, msg):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return "skip (token/chat id kosong)"
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": f"{title}\n{msg}"[:4000]}, timeout=15)
        return "ok" if r.ok else f"error {r.status_code}"
    except Exception as e:  # noqa: BLE001  (hanya nama error: pesan aslinya memuat token)
        return f"error: {type(e).__name__}"


def send(title, msg, cfg=None, force=False):
    if is_paused() and not force:
        return {"paused": True}
    channels = ((cfg or {}).get("notify") or {}).get("channels", ["macos", "telegram"])
    fn = {"macos": _macos, "telegram": _telegram}
    return {c: fn[c](title, msg) for c in channels if c in fn}


# ---------- anti-spam ----------
def _norm(reason):
    return _NUM.sub("#", reason)


def _num(reason):
    m = _NUM.search(reason)
    return float(m.group().rstrip("%")) if m else None


def should_send(key, value=None, cooldown=3):
    s = _load().get(key)
    if not s:
        return True
    if (date.today() - date.fromisoformat(s["date"])).days >= cooldown:
        return True
    # kirim ulang bila kondisi memburuk (besarnya naik >= 3 poin)
    return value is not None and s.get("value") is not None and abs(value) >= abs(s["value"]) + 3


def mark(key, value=None):
    s = _load()
    s[key] = {"date": date.today().isoformat(), "value": value}
    s = {k: v for k, v in s.items() if (date.today() - date.fromisoformat(v["date"])).days <= 30}
    _save(s)


def notify_triggers(trig, report_path, cfg=None):
    """Kirim hanya pemicu DARURAT yang baru/memburuk. Return hasil kirim atau None bila tidak ada yang baru."""
    cd = ((cfg or {}).get("notify") or {}).get("cooldown_days", 3)
    fresh = []
    for lvl, t, reason in trig:
        if lvl != "DARURAT":
            continue
        key, val = f"{t}|{_norm(reason)}", _num(reason)
        if should_send(key, val, cd):
            fresh.append((key, val, t, reason))
    if not fresh:
        return None
    msg = ("\n".join(f"{t}: {r}" for _, _, t, r in fresh)[:600]
           + f"\nLaporan: {Path(report_path).name}\nBuka Claude, unggah laporan, minta analisis darurat.")
    res = send("DARURAT Investasi", msg, cfg)
    if any(v == "ok" for v in res.values()):
        for key, val, _, _ in fresh:
            mark(key, val)
    return res


def notify_failure(problems, cfg=None):
    sent = []
    for p in problems:
        key = "FAIL|" + p.split(":")[0]
        s = _load().get(key)
        if s and s["date"] == date.today().isoformat():
            continue
        res = send("Sistem investasi bermasalah", p[:300], cfg)
        if any(v == "ok" for v in res.values()):
            mark(key)
        sent.append((p, res))
    return sent


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pause":
        PAUSE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE.write_text("jeda", encoding="utf-8")
        print("Notifikasi dijeda. Job harian tetap jalan. Lanjutkan: python src/notify.py resume")
    elif cmd == "resume":
        PAUSE.unlink(missing_ok=True)
        print("Notifikasi aktif lagi.")
    elif cmd == "test":
        print(send("Tes sistem investasi", "Kalau ini muncul, notifikasi bekerja.", force=True))
    else:
        print(__doc__)
