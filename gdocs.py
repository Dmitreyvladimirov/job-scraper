import logging
import os
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from config import RESUME_TEMPLATE_DOC_ID

logger = logging.getLogger(__name__)

FOLDER_ID = "1st2mew7eMqV8B6kCVDApsV69s05KXkbi"
TOKEN_URI  = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

PLACEHOLDER_KEYS = [
    "SUBTITLE", "ABOUT_ME",
    "SKILL_1", "SKILL_2", "SKILL_3", "SKILL_4",
    "IC_INTRO", "IC_B1", "IC_B2", "IC_B3", "IC_B4", "IC_B5",
    "SF_INTRO", "SF_B1", "SF_B2", "SF_B3", "SF_B4", "SF_B5", "SF_B6",
    "GB_INTRO", "GB_B1", "GB_B2", "GB_B3", "GB_B4",
]


def _services():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri=TOKEN_URI,
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    drive = build("drive", "v3", credentials=creds)
    docs  = build("docs",  "v1", credentials=creds)
    return drive, docs


def create_resume_doc(company: str, content: dict) -> str:
    """
    Copy the resume template and fill in adapted content via replaceAllText.
    content: dict returned by resume_generator.generate()
    Returns the edit URL.
    """
    drive, docs = _services()

    safe_company = "".join(c for c in company if c.isalnum() or c in " _-").strip()
    file_name = f"DimitryKucher_PM_{safe_company}"

    # Copy template
    copy = drive.files().copy(
        fileId=RESUME_TEMPLATE_DOC_ID,
        body={"name": file_name, "parents": [FOLDER_ID]},
        fields="id,webViewLink",
    ).execute()
    doc_id  = copy["id"]
    doc_url = copy["webViewLink"]
    logger.info(f"Doc copied from template: {file_name} id={doc_id}")

    # Build replaceAllText requests — skip empty strings (unused bullet slots)
    requests = []
    for key in PLACEHOLDER_KEYS:
        value = content.get(key, "").strip()
        if value:
            requests.append({"replaceAllText": {
                "containsText": {"text": f"{{{{{key}}}}}", "matchCase": True},
                "replaceText": value,
            }})
        else:
            # Remove placeholder entirely (bullet slot not used)
            requests.append({"replaceAllText": {
                "containsText": {"text": f"{{{{{key}}}}}", "matchCase": True},
                "replaceText": "—",  # dash so bullet line isn't blank
            }})

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()

    logger.info(f"Content inserted: {len(requests)} replacements")
    return doc_url
