"""Append per-run stats to Google Sheets after each scraper run."""
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

SCOPES = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
CREDS_JSON = os.environ.get("GOOGLE_SHEETS_CREDS", "")

HEADERS = [
    "timestamp", "total_fetched", "qualified",
    "low_score", "role", "location", "stale", "dedup", "gpt_limit", "gpt_calls",
    "Himalayas", "WeWorkRemotely", "Remotive", "Jobicy", "RemoteOK", "Arbeitnow",
]


def _get_access_token(creds: dict) -> str:
    import math
    now = int(time.time())
    payload = {
        "iss": creds["client_email"],
        "scope": SCOPES,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    # Build JWT manually (no external JWT library needed)
    import base64
    import hashlib
    import hmac
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    signing_input = header + b"." + body

    private_key = serialization.load_pem_private_key(
        creds["private_key"].encode(), password=None, backend=default_backend()
    )
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=")
    jwt = (signing_input + b"." + sig_b64).decode()

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _sheets_request(method: str, path: str, token: str, **kwargs) -> dict:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{path}"
    r = requests.request(method, url, headers={"Authorization": f"Bearer {token}"}, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json()


def _ensure_header(token: str) -> None:
    data = _sheets_request("GET", "/values/Stats!A1:Z1", token)
    existing = data.get("values", [[]])[0] if data.get("values") else []
    if existing != HEADERS:
        _sheets_request("PUT", "/values/Stats!A1", token, params={"valueInputOption": "RAW"},
                        json={"values": [HEADERS]})


def log_run(counts: dict, gpt_calls: int, source_counts: dict, started_at: str) -> None:
    """Append one row to the Stats sheet. Silently skips if not configured."""
    if not SHEET_ID or not CREDS_JSON:
        return
    try:
        creds = json.loads(CREDS_JSON)
        token = _get_access_token(creds)
        _ensure_header(token)
        row = [
            started_at,
            source_counts.get("_total", sum(source_counts.values())),
            counts.get("qualified", 0),
            counts.get("score", 0),
            counts.get("role", 0),
            counts.get("location", 0),
            counts.get("stale", 0),
            counts.get("dedup", 0),
            counts.get("gpt_limit", 0),
            gpt_calls,
            source_counts.get("Himalayas", 0),
            source_counts.get("WeWorkRemotely", 0),
            source_counts.get("Remotive", 0),
            source_counts.get("Jobicy", 0),
            source_counts.get("RemoteOK", 0),
            source_counts.get("Arbeitnow", 0),
        ]
        _sheets_request("POST", "/values/Stats!A1:append", token,
                        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                        json={"values": [row]})
        logger.info("Sheets: run stats logged")
    except Exception as e:
        logger.warning(f"Sheets: failed to log run — {e}")
