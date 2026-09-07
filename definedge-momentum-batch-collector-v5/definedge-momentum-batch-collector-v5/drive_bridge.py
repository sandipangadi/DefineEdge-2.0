import io
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
IST = ZoneInfo("Asia/Kolkata")


def _credentials():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON in Render environment.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _service():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


def latest_zip_from_folder(folder_id):
    if not folder_id:
        raise RuntimeError("Missing GOOGLE_DRIVE_INBOX_FOLDER_ID.")
    service = _service()
    result = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'",
        orderBy="modifiedTime desc",
        pageSize=50,
        fields="files(id,name,mimeType,modifiedTime,size)",
    ).execute()
    candidates = [f for f in result.get("files", []) if f.get("name", "").lower().endswith(".zip")]
    if not candidates:
        raise RuntimeError("No AlgoStra ZIP found in the configured Google Drive inbox.")
    src = candidates[0]
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=src["id"]))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return src, buf.getvalue()


def _ensure_date_folder(parent_id, date_text):
    service = _service()
    escaped = date_text.replace("'", "\\'")
    result = service.files().list(
        q=(f"'{parent_id}' in parents and trashed=false and "
           f"mimeType='application/vnd.google-apps.folder' and name='{escaped}'"),
        pageSize=10,
        fields="files(id,name)",
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    created = service.files().create(
        body={"name": date_text, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id,name",
    ).execute()
    return created["id"]


def publish_package(zip_path, evidence_parent_id, status_parent_id=None, source_meta=None, job_summary=None):
    if not evidence_parent_id:
        raise RuntimeError("Missing GOOGLE_DRIVE_EVIDENCE_FOLDER_ID.")
    service = _service()
    now_ist = datetime.now(IST)
    day = now_ist.date().isoformat()
    day_folder_id = _ensure_date_folder(evidence_parent_id, day)
    path = Path(zip_path)
    uploaded = service.files().create(
        body={"name": path.name, "parents": [day_folder_id]},
        media_body=MediaFileUpload(str(path), mimetype="application/zip", resumable=True),
        fields="id,name,webViewLink,createdTime,modifiedTime,size",
    ).execute()

    manifest = {
        "pipeline_version": "6.0",
        "created_at_ist": now_ist.isoformat(),
        "source": source_meta or {},
        "analysis_package": uploaded,
        "job_summary": job_summary or {},
        "status": "ready_for_trading_brain",
    }
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    manifest_name = path.stem + "_manifest.json"
    target_parent = status_parent_id or day_folder_id
    service.files().create(
        body={"name": manifest_name, "parents": [target_parent]},
        media_body=MediaIoBaseUpload(io.BytesIO(manifest_bytes), mimetype="application/json", resumable=False),
        fields="id,name,webViewLink",
    ).execute()
    return uploaded, manifest
