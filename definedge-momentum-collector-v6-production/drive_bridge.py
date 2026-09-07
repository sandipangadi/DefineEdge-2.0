import io
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
IST = ZoneInfo("Asia/Kolkata")


def _service_account_credentials():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON in Render environment.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _oauth_config():
    return {
        "client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
        "refresh_token": os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip(),
    }


def oauth_write_configured():
    cfg = _oauth_config()
    return all(cfg.values())


def drive_auth_mode():
    if oauth_write_configured():
        return "user_oauth"
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        return "service_account_read_only"
    return "unconfigured"


def _user_credentials():
    cfg = _oauth_config()
    missing = [name for name, value in cfg.items() if not value]
    if missing:
        env_names = {
            "client_id": "GOOGLE_OAUTH_CLIENT_ID",
            "client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
            "refresh_token": "GOOGLE_OAUTH_REFRESH_TOKEN",
        }
        raise RuntimeError(
            "Google Drive user OAuth write access is not configured. Missing: "
            + ", ".join(env_names[name] for name in missing)
            + ". The service account can read shared My Drive files but cannot upload new files because service accounts have no My Drive storage quota."
        )

    return UserCredentials(
        token=None,
        refresh_token=cfg["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        scopes=SCOPES,
    )


def _service(credentials):
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _read_service():
    # Preserve the proven shared-folder ingestion path. The service account can
    # read files shared with it even though it cannot own new My Drive uploads.
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        return _service(_service_account_credentials())
    return _service(_user_credentials())


def _write_service():
    # My Drive uploads must be owned by the user's Google account, so writes use
    # OAuth refresh-token credentials rather than the quota-less service account.
    return _service(_user_credentials())


def latest_zip_from_folder(folder_id):
    if not folder_id:
        raise RuntimeError("Missing GOOGLE_DRIVE_INBOX_FOLDER_ID.")

    service = _read_service()
    result = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'",
        orderBy="modifiedTime desc",
        pageSize=50,
        fields="files(id,name,mimeType,modifiedTime,size)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    visible_files = result.get("files", [])
    candidates = [
        item
        for item in visible_files
        if item.get("name", "").lower().endswith(".zip")
    ]

    if not candidates:
        visible_names = [item.get("name", "") for item in visible_files[:10]]
        detail = (
            " Visible files: " + ", ".join(visible_names)
            if visible_names
            else " The configured Drive identity sees no files in that folder."
        )
        raise RuntimeError(
            "No AlgoStra ZIP found in the configured Google Drive inbox."
            + detail
        )

    src = candidates[0]
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf,
        service.files().get_media(fileId=src["id"], supportsAllDrives=True),
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()

    return src, buf.getvalue()


def _ensure_date_folder(service, parent_id, date_text):
    escaped = date_text.replace("'", "\\'")
    result = service.files().list(
        q=(
            f"'{parent_id}' in parents and trashed=false and "
            f"mimeType='application/vnd.google-apps.folder' and name='{escaped}'"
        ),
        pageSize=10,
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = result.get("files", [])
    if files:
        return files[0]["id"]

    created = service.files().create(
        body={
            "name": date_text,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id,name",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def publish_package(
    zip_path,
    evidence_parent_id,
    status_parent_id=None,
    source_meta=None,
    job_summary=None,
):
    if not evidence_parent_id:
        raise RuntimeError("Missing GOOGLE_DRIVE_EVIDENCE_FOLDER_ID.")

    service = _write_service()
    now_ist = datetime.now(IST)
    day = now_ist.date().isoformat()
    day_folder_id = _ensure_date_folder(service, evidence_parent_id, day)
    path = Path(zip_path)

    uploaded = service.files().create(
        body={"name": path.name, "parents": [day_folder_id]},
        media_body=MediaFileUpload(
            str(path),
            mimetype="application/zip",
            resumable=True,
        ),
        fields="id,name,webViewLink,createdTime,modifiedTime,size",
        supportsAllDrives=True,
    ).execute()

    manifest = {
        "pipeline_version": "6.2",
        "created_at_ist": now_ist.isoformat(),
        "drive_auth_mode": "user_oauth",
        "source": source_meta or {},
        "analysis_package": uploaded,
        "job_summary": job_summary or {},
        "status": "ready_for_trading_brain",
    }

    manifest_bytes = json.dumps(
        manifest,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    manifest_name = path.stem + "_manifest.json"
    target_parent = status_parent_id or day_folder_id

    service.files().create(
        body={"name": manifest_name, "parents": [target_parent]},
        media_body=MediaIoBaseUpload(
            io.BytesIO(manifest_bytes),
            mimetype="application/json",
            resumable=False,
        ),
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()

    return uploaded, manifest
