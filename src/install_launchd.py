"""Pasang/lepas jadwal harian di macOS (launchd). Jalankan dengan Python dari venv-mu.

  python src/install_launchd.py install [--time 06:00]
  python src/install_launchd.py status
  python src/install_launchd.py uninstall
  python src/install_launchd.py print          (tampilkan plist saja, tidak memasang)

Jadwal: Selasa-Sabtu pukul --time waktu Mac (default 06:00). launchd menjalankan jadwal yang terlewat
saat Mac tidur begitu ia bangun; RunAtLoad menjalankan sekali saat login (job sendiri mencegah dobel).
"""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from db import ROOT

LABEL = "com.investadvisor.daily"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build(hour, minute):
    (ROOT / "logs").mkdir(exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "src" / "daily_job.py")],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": [{"Weekday": d, "Hour": hour, "Minute": minute} for d in (2, 3, 4, 5, 6)],
        "RunAtLoad": True,
        "StandardOutPath": str(ROOT / "logs" / "launchd.out"),
        "StandardErrorPath": str(ROOT / "logs" / "launchd.err"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"},
    }


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["install", "uninstall", "status", "print"])
    ap.add_argument("--time", default="06:00")
    a = ap.parse_args()
    h, m = (int(x) for x in a.time.split(":"))
    if a.cmd == "print":
        print(plistlib.dumps(build(h, m)).decode())
        return
    if sys.platform != "darwin":
        sys.exit("Perintah ini hanya untuk macOS.")
    domain = f"gui/{os.getuid()}"
    if a.cmd == "install":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(build(h, m)))
        launchctl("bootout", domain, str(PLIST))
        r = launchctl("bootstrap", domain, str(PLIST))
        print("Terpasang." if r.returncode == 0 else f"Gagal bootstrap: {r.stderr.strip()}")
        print(f"File: {PLIST}\nPython: {sys.executable}\nJadwal: Selasa-Sabtu {a.time}")
    elif a.cmd == "uninstall":
        launchctl("bootout", domain, str(PLIST))
        PLIST.unlink(missing_ok=True)
        print("Jadwal dilepas.")
    else:
        r = launchctl("print", f"{domain}/{LABEL}")
        print(r.stdout[:1500] if r.returncode == 0 else "Belum terpasang.")


if __name__ == "__main__":
    main()
