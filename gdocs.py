import json
import logging
import os
import urllib.request
import urllib.parse
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

FOLDER_ID = "1st2mew7eMqV8B6kCVDApsV69s05KXkbi"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]


def _services():
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "Missing Google OAuth env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN"
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    return drive, docs


def create_resume_doc(company: str, html_content: str) -> str:
    """
    Create an adapted resume Google Doc for the given company.
    Uses OAuth credentials (user's own account) — no storage quota issues.
    Returns the edit URL.
    """
    drive, docs = _services()

    safe_company = "".join(c for c in company if c.isalnum() or c in " _-").strip()
    file_name = f"DimitryKucher_PM_{safe_company}"

    # Create empty Google Doc (metadata only, no upload)
    doc = docs.documents().create(body={"title": file_name}).execute()
    doc_id = doc["documentId"]
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    logger.info(f"Doc created: {file_name} id={doc_id}")

    # Move into the target folder
    file_meta = drive.files().get(fileId=doc_id, fields="parents").execute()
    previous_parents = ",".join(file_meta.get("parents", []))
    drive.files().update(
        fileId=doc_id,
        addParents=FOLDER_ID,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()

    # Insert resume content via Docs API
    try:
        text = _html_to_text(html_content)
        if text:
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]},
            ).execute()
            logger.info(f"Content inserted: {len(text)} chars")
    except Exception as e:
        logger.warning(f"Content insertion failed (doc still accessible): {e}")

    return doc_url


def _html_to_text(html: str) -> str:
    """Strip HTML tags, preserve structure as plain text."""
    import re
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'</?(h[1-6]|p|li|ul|ol|div)[^>]*>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text + "\n"
