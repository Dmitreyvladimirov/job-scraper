import json
import logging
import os
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

FOLDER_ID = "1st2mew7eMqV8B6kCVDApsV69s05KXkbi"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]


def _services():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise EnvironmentError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    return drive, docs


def create_resume_doc(company: str, html_content: str) -> str:
    """
    Upload adapted resume HTML as a Google Doc in FOLDER_ID.
    Name: DimitryKucher_PM_{company}
    Returns the edit URL of the created doc.
    """
    drive, _ = _services()

    # Sanitise company name for filename
    safe_company = "".join(c for c in company if c.isalnum() or c in " _-").strip()
    file_name = f"DimitryKucher_PM_{safe_company}"

    # Drive API converts HTML → Google Doc when mimeType is set
    file_metadata = {
        "name": file_name,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [FOLDER_ID],
    }

    from googleapiclient.http import MediaInMemoryUpload
    media = MediaInMemoryUpload(
        html_content.encode("utf-8"),
        mimetype="text/html",
        resumable=False,
    )

    file = drive.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,webViewLink",
    ).execute()

    doc_url = file.get("webViewLink", f"https://docs.google.com/document/d/{file['id']}/edit")
    logger.info(f"Google Doc created: {file_name} → {doc_url}")
    return doc_url
