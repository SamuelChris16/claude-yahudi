"""Satu pintu untuk semua request keluar: sensor rahasia, timeout, dan retry.

Pesan error dari `requests` memuat URL lengkap (termasuk apikey/token). Semua modul memakai
get()/post() di sini supaya rahasia tidak pernah masuk ke log, terminal, atau notifikasi.
"""
import os
import re
import time

import requests

SECRET_ENV = ("ALPHAVANTAGE_API_KEY", "FINNHUB_API_KEY", "FRED_API_KEY", "STOOQ_API_KEY",
              "TELEGRAM_BOT_TOKEN", "GDRIVE_FOLDER_ID")
_PARAM = re.compile(r"(?i)\b(apikey|api_key|token|access_token|key)=([^&\s\"']+)")
_BOT = re.compile(r"(?i)/bot\d+:[A-Za-z0-9_\-]+")


class HttpError(Exception):
    """Kegagalan jaringan/HTTP dengan pesan yang sudah disensor."""


def redact(text):
    s = str(text)
    for name in SECRET_ENV:
        v = os.getenv(name)
        if v and len(v) >= 6:
            s = s.replace(v, "***")
    s = _PARAM.sub(lambda m: m.group(1) + "=***", s)
    return _BOT.sub("/bot***", s)


def _request(method, url, retries, backoff, **kw):
    kw.setdefault("timeout", 30)
    if os.getenv("INVEST_OFFLINE"):      # dipakai tes otomatis: jangan pernah menyentuh jaringan
        raise HttpError("mode offline (INVEST_OFFLINE): request ke %s dilewati" % redact(url.split("?")[0]))
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                last = "HTTP %d" % r.status_code
            else:
                if r.status_code >= 400:
                    raise HttpError("HTTP %d dari %s" % (r.status_code, redact(url.split("?")[0])))
                return r
        except requests.RequestException as e:
            last = "%s: %s" % (type(e).__name__, redact(e))
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise HttpError("%s (%s)" % (last, redact(url.split("?")[0])))


def get(url, params=None, headers=None, timeout=30, retries=2, backoff=2.0):
    return _request("GET", url, retries, backoff, params=params, headers=headers, timeout=timeout)


def post(url, data=None, timeout=15, retries=1, backoff=2.0):
    return _request("POST", url, retries, backoff, data=data, timeout=timeout)
