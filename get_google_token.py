"""
One-time script to get a Google OAuth refresh token.

Usage:
  python3 get_google_token.py /path/to/client_secret.json

After running: copy the printed values to GitHub secrets:
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN
"""
import json
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 get_google_token.py /path/to/client_secret.json")
        sys.exit(1)

    client_secret_path = sys.argv[1]
    if not Path(client_secret_path).exists():
        print(f"File not found: {client_secret_path}")
        sys.exit(1)

    print("Opening browser for Google authorization...")
    print("Log in with dmitreyvladimirovic@gmail.com and grant access.\n")

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    creds = flow.run_local_server(port=0)

    client_info = json.loads(Path(client_secret_path).read_text())
    key = list(client_info.keys())[0]  # "web" or "installed"
    client_id = client_info[key]["client_id"]
    client_secret = client_info[key]["client_secret"]

    print("\n=== Add these 3 values as GitHub Actions secrets ===\n")
    print(f"GOOGLE_CLIENT_ID:\n{client_id}\n")
    print(f"GOOGLE_CLIENT_SECRET:\n{client_secret}\n")
    print(f"GOOGLE_REFRESH_TOKEN:\n{creds.refresh_token}\n")
    print("=====================================================")
    print("Done. You can delete client_secret.json after saving the values above.")

if __name__ == "__main__":
    main()
