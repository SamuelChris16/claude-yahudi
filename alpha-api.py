import pandas as pd
import requests
import os
import time
from dotenv import load_dotenv

# 1. Load API Key dari file .env
load_dotenv()
API_KEY = os.getenv("ALPHA_VANTAGE_KEY")

# 2. Parameter Pengaturan
TICKERS = ["AAPL", "TSLA", "NVDA", "MSFT"] # Daftar saham yang ingin diunduh
HARI_KE_BELAKANG = 30                      # Batasi 30 hari agar Claude tidak kelebihan beban
OUTPUT_DIR = "data"
OUTPUT_FILE = f"{OUTPUT_DIR}/gabungan_saham_claude.csv"

def fetch_and_merge_stocks():
    # Pastikan folder 'data' ada
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    semua_data = []

    print("Memulai proses unduhan...\n")
    
    for i, ticker in enumerate(TICKERS):
        print(f"[{ticker}] Mengambil data harga harian...")
        url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={ticker}&apikey={API_KEY}"
        
        response = requests.get(url)
        data = response.json()
        
        if "Time Series (Daily)" in data:
            # Ubah JSON ke Pandas DataFrame
            time_series = data["Time Series (Daily)"]
            df = pd.DataFrame.from_dict(time_series, orient='index')
            df.columns = ['Open (USD)', 'High (USD)', 'Low (USD)', 'Close (USD)', 'Volume']
            df.index.name = 'Date'
            df.reset_index(inplace=True)
            
            # Tambahkan kolom Ticker agar Claude tahu ini saham apa
            df.insert(0, 'Ticker', ticker)
            df.sort_values('Date', ascending=False, inplace=True)
            
            # Potong data sesuai jumlah hari
            df = df.head(HARI_KE_BELAKANG)
            semua_data.append(df)
            print(f"  ✅ Sukses: {len(df)} baris.")
        else:
            print(f"  ❌ Gagal mengambil data {ticker}. Pesan: {data}")
            
        # Jeda 25 detik untuk menghindari Rate Limit Alpha Vantage Gratis (5 call / menit)
        if i < len(TICKERS) - 1:
            print("  ⏳ Menunggu jeda 25 detik...")
            time.sleep(25)

    # 3. Proses Penggabungan
    if semua_data:
        df_gabungan = pd.concat(semua_data, ignore_index=True)
        # Urutkan berdasarkan Ticker lalu Tanggal
        df_gabungan.sort_values(by=['Ticker', 'Date'], ascending=[True, False], inplace=True)
        
        # Simpan ke dalam folder data/
        df_gabungan.to_csv(OUTPUT_FILE, index=False)
        print(f"\n🎉 Selesai! Semua data berhasil digabung menjadi 1 file di: '{OUTPUT_FILE}'")
    else:
        print("\n⚠️ Proses selesai, tetapi tidak ada data yang berhasil diunduh.")

if __name__ == "__main__":
    fetch_and_merge_stocks()