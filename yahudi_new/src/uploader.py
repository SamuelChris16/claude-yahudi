"""Unggah berkas ke Google Drive lewat Drive API (opsional; bawaan sistem adalah salin ke folder Drive).

Butuh paket tambahan:  pip install google-api-python-client google-auth-oauthlib
dan credentials/client_secret.json (OAuth Client ID tipe Desktop dari Google Cloud).
Login browser hanya saat pertama; sesudahnya memakai credentials/token.json.
Scope drive.file: aplikasi hanya bisa melihat berkas yang dibuatnya sendiri.
"""
import mimetypes
import os

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _service(credentials_path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError("paket Google belum terpasang: pip install google-api-python-client google-auth-oauthlib")
    if not os.path.exists(credentials_path):
        raise RuntimeError("berkas OAuth Client ID tidak ditemukan di %s" % credentials_path)
    token_path = os.path.join(os.path.dirname(credentials_path), "token.json")
    creds = Credentials.from_authorized_user_file(token_path, SCOPES) if os.path.exists(token_path) else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES).run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def upload_file(file_path, folder_id, credentials_path):
    """Unggah berkas. Bila berkas bernama sama (buatan aplikasi ini) sudah ada di folder, isinya diperbarui
    alih-alih membuat duplikat. Return ID berkas di Drive."""
    from googleapiclient.http import MediaFileUpload
    service = _service(credentials_path)
    name = os.path.basename(file_path)
    mime = mimetypes.guess_type(name)[0] or ("text/markdown" if name.endswith(".md") else "application/octet-stream")
    media = MediaFileUpload(file_path, mimetype=mime, resumable=True)
    q = "name = '%s' and '%s' in parents and trashed = false" % (name.replace("'", "\\'"), folder_id)
    found = service.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    if found:
        return service.files().update(fileId=found[0]["id"], media_body=media, fields="id").execute().get("id")
    return service.files().create(body={"name": name, "parents": [folder_id]}, media_body=media, fields="id").execute().get("id")


def upload_csv_to_drive(file_path, folder_id, credentials_path):
    """Nama lama, dipertahankan agar pemanggil lama tetap jalan."""
    try:
        print("[SUCCESS] Berkas diunggah. Drive File ID: %s" % upload_file(file_path, folder_id, credentials_path))
    except Exception as e:  # noqa: BLE001
        print("[ERROR] Gagal mengunggah ke Google Drive: %s" % e)
