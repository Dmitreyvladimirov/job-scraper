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

SKILL_KEYS = {"SKILL_1", "SKILL_2", "SKILL_3", "SKILL_4"}
EMPTY_MARKER = "​"  # zero-width space — marks unused bullet slots for deletion


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


def _hide_empty_bullets(docs, doc_id: str):
    """Make unused bullet slots invisible: 1pt white font + zero paragraph spacing."""
    doc = docs.documents().get(documentId=doc_id).execute()
    requests = []

    def scan(content):
        for elem in content:
            if "paragraph" in elem:
                text = "".join(
                    r.get("textRun", {}).get("content", "")
                    for r in elem["paragraph"]["elements"]
                ).strip()
                if text == EMPTY_MARKER:
                    requests.append({"updateTextStyle": {
                        "range": {
                            "startIndex": elem["startIndex"],
                            "endIndex": elem["endIndex"] - 1,
                        },
                        "textStyle": {
                            "fontSize": {"magnitude": 1, "unit": "PT"},
                            "foregroundColor": {
                                "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}
                            },
                        },
                        "fields": "fontSize,foregroundColor",
                    }})
                    # Collapse vertical space so hidden line takes ~0 height
                    requests.append({"updateParagraphStyle": {
                        "range": {
                            "startIndex": elem["startIndex"],
                            "endIndex": elem["endIndex"],
                        },
                        "paragraphStyle": {
                            "spaceAbove": {"magnitude": 0, "unit": "PT"},
                            "spaceBelow": {"magnitude": 0, "unit": "PT"},
                        },
                        "fields": "spaceAbove,spaceBelow",
                    }})
            elif "table" in elem:
                for row in elem["table"]["tableRows"]:
                    for cell in row["tableCells"]:
                        scan(cell["content"])

    scan(doc["body"]["content"])

    if requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()
        logger.info(f"Hidden {len(requests) // 2} empty bullet lines")


def _format_skills(docs, doc_id: str):
    """
    For each skill paragraph (Category: skill list), apply:
    - Category name (up to and including ':'): bold, current size
    - Skill list (after ':'): normal weight, 8.5pt
    """
    doc = docs.documents().get(documentId=doc_id).execute()
    requests = []

    def scan(content):
        for elem in content:
            if "paragraph" in elem:
                para = elem["paragraph"]
                full_text = "".join(
                    r.get("textRun", {}).get("content", "")
                    for r in para["elements"]
                )
                # Skill paragraphs: "Category Name: skill1, skill2, ..."
                # Guard: require comma-separated list after colon to avoid bolding
                # bullet text that happens to contain a colon (e.g. "Launched X: result")
                colon_pos = full_text.find(":")
                if (colon_pos > 0
                        and elem["startIndex"] > 0
                        and "," in full_text[colon_pos + 1:]):
                    colon_offset = colon_pos
                    para_start = elem["startIndex"]
                    colon_abs = para_start + colon_offset

                    # Bold the category name (inclusive of colon)
                    requests.append({"updateTextStyle": {
                        "range": {
                            "startIndex": para_start,
                            "endIndex": colon_abs + 1,
                        },
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }})

                    # Normal weight for the skill list (keep template font size)
                    list_end = elem["endIndex"] - 1  # exclude newline
                    if colon_abs + 1 < list_end:
                        requests.append({"updateTextStyle": {
                            "range": {
                                "startIndex": colon_abs + 1,
                                "endIndex": list_end,
                            },
                            "textStyle": {
                                "bold": False,
                            },
                            "fields": "bold",
                        }})

            elif "table" in elem:
                for row in elem["table"]["tableRows"]:
                    for cell_idx, cell in enumerate(row["tableCells"]):
                        if cell_idx == 0:  # skills are in left column only
                            scan(cell["content"])

    scan(doc["body"]["content"])

    if requests:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests},
        ).execute()
        logger.info(f"Skills formatting applied: {len(requests)} style requests")


def _format_about_me(docs, doc_id: str, about_text: str):
    """Set About Me paragraph to 9.5pt (template placeholder may inherit smaller size)."""
    if not about_text:
        return
    doc = docs.documents().get(documentId=doc_id).execute()
    search_prefix = about_text.strip()[:30].lower()
    requests = []

    def scan(content):
        for elem in content:
            if "paragraph" in elem:
                text = "".join(
                    r.get("textRun", {}).get("content", "")
                    for r in elem["paragraph"]["elements"]
                )
                if search_prefix in text.lower() and elem["endIndex"] > elem["startIndex"] + 1:
                    requests.append({"updateTextStyle": {
                        "range": {
                            "startIndex": elem["startIndex"],
                            "endIndex": elem["endIndex"] - 1,
                        },
                        "textStyle": {
                            "fontSize": {"magnitude": 9.5, "unit": "PT"},
                        },
                        "fields": "fontSize",
                    }})
            elif "table" in elem:
                for row in elem["table"]["tableRows"]:
                    for cell_idx, cell in enumerate(row["tableCells"]):
                        if cell_idx == 1:  # right column only
                            scan(cell["content"])

    scan(doc["body"]["content"])
    if requests:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        logger.info("About Me font size set to 9.5pt")


def create_resume_doc(company: str, content: dict) -> str:
    """
    Copy the resume template and fill in adapted content via replaceAllText.
    Then applies skills formatting and removes unused bullet slots.
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

    # Replace all placeholders
    requests = []
    for key in PLACEHOLDER_KEYS:
        value = (content.get(key) or "").strip()
        requests.append({"replaceAllText": {
            "containsText": {"text": f"{{{{{key}}}}}", "matchCase": True},
            "replaceText": value if value else EMPTY_MARKER,
        }})

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()
    logger.info(f"Content inserted: {len(requests)} replacements")

    # Post-processing: hide empty bullet lines, fix skills formatting
    try:
        _hide_empty_bullets(docs, doc_id)
    except Exception as e:
        logger.warning(f"Empty bullet hide failed (non-critical): {e}")

    try:
        _format_skills(docs, doc_id)
    except Exception as e:
        logger.warning(f"Skills formatting failed (non-critical): {e}")

    try:
        _format_about_me(docs, doc_id, content.get("ABOUT_ME", ""))
    except Exception as e:
        logger.warning(f"About Me formatting failed (non-critical): {e}")

    return doc_url
