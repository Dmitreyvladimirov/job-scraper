"""Mint the Gmail refresh token once, locally, and store it encrypted.

Run this on Dimitry's Mac; the cloud agent never sees a browser. Produces:
  - a Fernet key (GMAIL_TOKEN_KEY) if there isn't one,
  - a row in google_credential with the encrypted refresh token.

Prerequisites (one-time, in Google Cloud Console):
  1. Create a project, enable the Gmail API.
  2. OAuth consent screen: External, add yourself as a test user, then PUBLISH it —
     while the app sits in Testing, refresh tokens die after 7 days, which fails
     silently and looks exactly like "nobody emailed me".
  3. Credentials → OAuth client ID → type "Desktop app". Copy the client id/secret.

Usage:
    export GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=...
    python scripts/mint_gmail_token.py

The only scope requested is gmail.readonly: the agent cannot send, delete or label.
"""
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import db  # noqa: E402
from gmail_client import SCOPE, TOKEN_URL  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT = "http://localhost:8765/"
_result = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _result.update({k: v[0] for k, v in params.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>Готово — можно закрыть вкладку.</h2>".encode())

    def log_message(self, *args):
        pass


def main() -> None:
    client_id = os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first (see docstring).")

    state = secrets.token_urlsafe(16)
    auth = f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })

    server = http.server.HTTPServer(("localhost", 8765), _Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    print("Открываю браузер для входа в Google…")
    webbrowser.open(auth)
    threading.Event().wait(180) if not _result else None
    server.server_close()

    if _result.get("state") != state:
        sys.exit("State mismatch — aborting.")
    code = _result.get("code")
    if not code:
        sys.exit(f"No authorization code returned: {_result}")

    resp = requests.post(TOKEN_URL, timeout=30, data={
        "client_id": client_id, "client_secret": client_secret, "code": code,
        "grant_type": "authorization_code", "redirect_uri": REDIRECT,
    })
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed: {resp.status_code} {resp.text[:300]}")
    refresh = resp.json().get("refresh_token")
    if not refresh:
        sys.exit("Google returned no refresh_token (re-run with prompt=consent).")

    from cryptography.fernet import Fernet
    key = os.environ.get("GMAIL_TOKEN_KEY") or Fernet.generate_key().decode()
    blob = Fernet(key.encode()).encrypt(refresh.encode())

    email = os.environ.get("GMAIL_EMAIL", "me")
    conn = db._conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO google_credential (user_id, email, refresh_token, scopes)
                   VALUES (NULL, %s, %s, %s)
                   ON CONFLICT (email) DO UPDATE
                   SET refresh_token = EXCLUDED.refresh_token, last_error = NULL""",
                (email, blob, SCOPE))
    conn.close()

    print("\nСохранено в google_credential. Добавь в Railway (сервис mail-agent):")
    print(f"  GMAIL_CLIENT_ID={client_id}")
    print("  GMAIL_CLIENT_SECRET=<тот же секрет>")
    print(f"  GMAIL_TOKEN_KEY={key}")
    print("\nRefresh-токен в базе, зашифрован. В переменные его класть не нужно.")


if __name__ == "__main__":
    main()
