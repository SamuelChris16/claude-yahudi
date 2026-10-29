# 📈 Claude Paper Trading Pipeline

Automasi *data pipeline* berbasis Python yang menarik data pasar saham harian dari Alpha Vantage, melakukan pembersihan dan penggabungan data, lalu mengunggahnya secara otomatis ke Google Drive. 

**Tujuan Utama:** Proyek ini dirancang khusus untuk menjembatani data pasar saham mentah dengan **Claude AI**. Dengan meletakkan dataset CSV langsung ke dalam Google Drive, Claude (melalui integrasi Google Drive-nya) dapat dengan mudah membaca, menganalisis, dan memberikan *insight paper trading* tanpa perlu intervensi manual setiap harinya.

---

## ✨ Fitur Utama

- **Automasi Tarik Data (Fetch):** Mengunduh data *Time Series Daily* dari berbagai emiten menggunakan Alpha Vantage API.
- **Manajemen Rate-Limit Cerdas:** Terdapat jeda dinamis antar-permintaan API untuk mencegah pemblokiran dari *free-tier* Alpha Vantage (maks 25 *request* / hari).
- **Pembersihan & Penggabungan Data (ETL):** Mengubah format JSON mentah menjadi Pandas DataFrame yang rapi, memotong data berdasarkan rentang hari, dan menggabungkannya ke dalam satu file CSV master.
- **Otentikasi Google Drive Pintar (OAuth 2.0):** Meminta izin *login* browser hanya pada **penggunaan pertama**. Setelahnya, sistem akan membuat `token.json` dan berjalan 100% otomatis di latar belakang (*background*).

---

## 📂 Struktur Repositori

```text
claude-yahudi/
├── data/                            # Folder output sementara untuk CSV (diabaikan Git)
├── credentials/                     # Folder untuk kredensial OAuth 2.0
│   ├── client_secret.json           # File rahasia dari Google Cloud (Unduh manual)
│   └── token.json                   # Dibuat otomatis saat login pertama kali
├── src/
│   ├── __init__.py                  
│   ├── fetcher.py                   # Modul logika penarikan data Alpha Vantage
│   └── uploader.py                  # Modul logika unggah ke Google Drive
├── .env                             # Variabel lingkungan (API Key & Folder ID)
├── .gitignore                       # Mencegah kebocoran kredensial
├── requirements.txt                 # Daftar library dependensi Python
└── main.py                          # Script utama/orkestrator pipeline