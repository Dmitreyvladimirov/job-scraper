"""Gmail reader for the mail agent — REST calls, no google client libraries.

Deliberately hand-rolled over `requests` instead of pulling google-api-python-client
+ google-auth (~30 transitive deps into a service whose whole job is two GET calls
and a token refresh). The refresh flow is a documented form POST; the messages API
is two REST endpoints.

Scope is `gmail.readonly` and nothing else: the agent is physically unable to send,
delete, or even label mail. "Already processed" is tracked by mail_event.message_id
in Postgres, which is why no write scope is needed at all.

Credentials: client id/secret + a refresh token minted ONCE locally with
scripts/mint_gmail_token.py. The refresh token is stored Fernet-encrypted in
google_credential (a row, not env — so adding a second user is a row, not a rewrite);
GMAIL_TOKEN_KEY holds the Fernet key.
"""
import base64
import logging
import os
import re
from datetime import datetime, timezone
from html import unescape

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailAuthError(Exception):
    """Refresh failed — the credential is dead until a human re-mints it. Kept
    distinct from transient errors because the reaction differs: this one has to
    reach Dimitry (a silently dead mail agent looks exactly like 'nobody replied')."""


def access_token(client_id: str, client_secret: str, refresh_token: str,
                 *, timeout: float = 20.0) -> str:
    resp = requests.post(TOKEN_URL, timeout=timeout, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    })
    if resp.status_code != 200:
        # 400 invalid_grant is the classic one: the OAuth app is still in Testing
        # mode, where refresh tokens expire after 7 days. Publishing the app fixes it.
        raise GmailAuthError(f"token refresh failed: HTTP {resp.status_code} {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        raise GmailAuthError("token refresh returned no access_token")
    return token


def _decode_part(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _plain_text(payload: dict) -> str:
    """text/plain only. Storing the HTML part would put attacker-controlled markup
    into card notes; plain text also classifies better (no boilerplate wrappers)."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    for part in payload.get("parts") or []:
        text = _plain_text(part)
        if text:
            return text
    if mime == "text/html":
        html = _decode_part(payload)
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()
    return ""


def _header(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch_messages(token: str, query: str, *, limit: int = 50,
                   timeout: float = 20.0) -> list[dict]:
    """Messages matching a Gmail search query, newest first, normalized for the agent.

    message_id is the RFC822 Message-ID header, NOT the Gmail id: it is globally
    unique, survives an account change, and is identical over IMAP — which keeps
    mail_event's idempotency key meaningful if the transport ever changes."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{API}/messages", headers=headers, timeout=timeout,
                        params={"q": query, "maxResults": min(limit, 100)})
    if resp.status_code == 401:
        raise GmailAuthError("Gmail rejected the access token")
    resp.raise_for_status()
    ids = [m["id"] for m in (resp.json().get("messages") or [])]

    out = []
    for gid in ids[:limit]:
        r = requests.get(f"{API}/messages/{gid}", headers=headers, timeout=timeout,
                         params={"format": "full"})
        if r.status_code == 401:
            raise GmailAuthError("Gmail rejected the access token mid-fetch")
        if r.status_code != 200:
            logger.warning(f"gmail: message {gid} returned HTTP {r.status_code}")
            continue
        msg = r.json()
        hdrs = (msg.get("payload") or {}).get("headers") or []
        internal = msg.get("internalDate")
        received = None
        if internal:
            received = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        out.append({
            "message_id": _header(hdrs, "Message-ID") or f"gmail:{gid}",
            "thread_id": msg.get("threadId"),
            "received_at": received,
            "from_addr": _header(hdrs, "From"),
            "subject": _header(hdrs, "Subject"),
            "excerpt": _plain_text(msg.get("payload") or {}).strip(),
        })
    return out


def load_credential(email: str | None = None) -> dict | None:
    """Credential row + decrypted refresh token, or None when unconfigured.
    Falls back to env vars so the agent can run before the row exists."""
    import db
    from cryptography.fernet import Fernet

    key = os.environ.get("GMAIL_TOKEN_KEY", "")
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            if email:
                cur.execute("SELECT * FROM google_credential WHERE email = %s", (email,))
            else:
                cur.execute("SELECT * FROM google_credential WHERE user_id IS NULL "
                            "ORDER BY id LIMIT 1")
            row = cur.fetchone()
    finally:
        conn.close()

    if row and key:
        try:
            token = Fernet(key.encode()).decrypt(bytes(row["refresh_token"])).decode()
            return {"email": row["email"], "refresh_token": token,
                    "client_id": os.environ.get("GMAIL_CLIENT_ID", ""),
                    "client_secret": os.environ.get("GMAIL_CLIENT_SECRET", "")}
        except Exception:
            logger.exception("gmail: stored refresh token could not be decrypted")

    env_token = os.environ.get("GMAIL_REFRESH_TOKEN", "")
    if env_token:
        return {"email": os.environ.get("GMAIL_EMAIL", "me"), "refresh_token": env_token,
                "client_id": os.environ.get("GMAIL_CLIENT_ID", ""),
                "client_secret": os.environ.get("GMAIL_CLIENT_SECRET", "")}
    return None
