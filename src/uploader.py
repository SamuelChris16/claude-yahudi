import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/drive.file']

def upload_csv_to_drive(file_path: str, folder_id: str, credentials_path: str) -> None:
    if not os.path.exists(credentials_path):
        print(f"[ERROR] File OAuth Client ID tidak ditemukan di {credentials_path}")
        return

    creds = None
    # Menyimpan sesi login di file token.json
    token_path = os.path.join(os.path.dirname(credentials_path), 'token.json')

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        
    # Jika belum ada sesi atau token expired, minta login via browser
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            
        # Simpan token untuk run berikutnya
        with open(token_path, 'w') as token:
            token.write(creds.to_json())

    print("[INFO] Mengautentikasi Google Drive (OAuth 2.0)...")
    try:
        service = build('drive', 'v3', credentials=creds)
        
        file_metadata = {
            'name': os.path.basename(file_path),
            'parents': [folder_id]
        }
        
        media = MediaFileUpload(file_path, mimetype='text/csv', resumable=True)
        
        print(f"[INFO] Memulai proses unggah {file_metadata['name']} ke Google Drive...")
        uploaded_file = service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id'
        ).execute()
        
        print(f"[SUCCESS] File berhasil diunggah. Drive File ID: {uploaded_file.get('id')}")
        
    except Exception as e:
        print(f"[ERROR] Kegagalan saat mengunggah ke Google Drive: {e}")