import os
import time
import requests
import pandas as pd
from typing import List, Optional

API_RATE_LIMIT_DELAY = 25

def fetch_daily_series(ticker: str, api_key: str, days_limit: int) -> Optional[pd.DataFrame]:
    url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={ticker}&apikey={api_key}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Kegagalan jaringan saat memproses {ticker}: {e}")
        return None

    if "Time Series (Daily)" not in data:
        print(f"[WARNING] Data time series tidak ditemukan untuk {ticker}.")
        return None

    time_series = data["Time Series (Daily)"]
    df = pd.DataFrame.from_dict(time_series, orient='index')
    
    df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
    df.index.name = 'Date'
    df.reset_index(inplace=True)
    
    df.insert(0, 'Ticker', ticker)
    df.sort_values('Date', ascending=False, inplace=True)
    
    return df.head(days_limit)

def process_and_merge_stocks(tickers: List[str], api_key: str, days_limit: int, output_path: str) -> bool:
    collected_data: List[pd.DataFrame] = []

    print(f"[INFO] Memulai ekstraksi data untuk {len(tickers)} emiten...")

    for index, ticker in enumerate(tickers):
        print(f"-> Mengunduh data {ticker}...")
        df_ticker = fetch_daily_series(ticker, api_key, days_limit)
        
        if df_ticker is not None and not df_ticker.empty:
            collected_data.append(df_ticker)
            print(f"   Berhasil: {len(df_ticker)} baris.")
        
        if index < len(tickers) - 1:
            print(f"   Jeda {API_RATE_LIMIT_DELAY} detik (Rate Limit API)...")
            time.sleep(API_RATE_LIMIT_DELAY)

    if collected_data:
        df_final = pd.concat(collected_data, ignore_index=True)
        df_final.sort_values(by=['Ticker', 'Date'], ascending=[True, False], inplace=True)
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        df_final.to_csv(output_path, index=False)
        print(f"[SUCCESS] Data berhasil disatukan di: {output_path}")
        return True
    
    print("[ERROR] Proses dibatalkan. Tidak ada data yang valid.")
    return False