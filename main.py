import os
from dotenv import load_dotenv
from src.fetcher import process_and_merge_stocks
from src.uploader import upload_csv_to_drive

# ATUR PARAMETER DISINI
TARGET_TICKERS = ["AAPL", "TSLA", "NVDA", "MSFT"]
HISTORICAL_DAYS_LIMIT = 30
OUTPUT_FILE_PATH = os.path.join("data", "gabungan_saham_claude.csv")
CREDENTIALS_PATH = os.path.join("credentials", "service_account.json")

def main() -> None:
    load_dotenv()
    api_key = os.getenv("ALPHA_VANTAGE_KEY")
    drive_folder_id = os.getenv("GDRIVE_FOLDER_ID")
    
    if not api_key or not drive_folder_id:
        print("[ERROR] Konfigurasi tidak lengkap. Periksa ALPHA_VANTAGE_KEY dan GDRIVE_FOLDER_ID di file .env")
        return

    print("=== PIPELINE PAPER TRADING DIMULAI ===")
    
    is_success = process_and_merge_stocks(
        tickers=TARGET_TICKERS,
        api_key=api_key,
        days_limit=HISTORICAL_DAYS_LIMIT,
        output_path=OUTPUT_FILE_PATH
    )
    
    if is_success:
        print("\n=== PROSES UNGGAH CLOUD ===")
        upload_csv_to_drive(
            file_path=OUTPUT_FILE_PATH,
            folder_id=drive_folder_id,
            credentials_path=CREDENTIALS_PATH
        )
    
    print("\n=== PIPELINE SELESAI ===")

if __name__ == "__main__":
    main()