"""Pintu masuk tunggal. Semua perintah tetap bisa dijalankan langsung lewat `python src/<nama>.py`.

  python main.py harian [--no-news]        ambil data + hitung (sekali jalan, manual)
  python main.py job [--force --dry ...]    job terjadwal lengkap (notifikasi, laporan, backup)
  python main.py laporan mingguan|darurat|kuartalan
  python main.py validasi                   periksa config + bangun ulang harga bersih
  python main.py split daftar|konfirmasi|tolak ...
  python main.py backfill [--tahun 5]       riwayat harga panjang dari Stooq
  python main.py transaksi init|cek|sinkron
  python main.py skema status|check
  python main.py notifikasi test|pause|resume
  python main.py jadwal install|status|uninstall
"""
import runpy
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
COMMANDS = {"harian": "run_daily", "job": "daily_job", "laporan": "report", "validasi": "validate",
            "split": "validate", "backfill": "backfill", "transaksi": "ledger", "skema": "schema",
            "notifikasi": "notify", "jadwal": "install_launchd"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return
    cmd = sys.argv[1]
    script = SRC / (COMMANDS[cmd] + ".py")
    sys.path.insert(0, str(SRC))
    sys.argv = [str(script)] + (["split"] if cmd == "split" else []) + sys.argv[2:]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
