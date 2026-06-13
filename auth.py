"""
Gmail OAuth 2.0 authentication.
Saves a token locally so re-authentication is only needed when the token expires
or is revoked. On first run, opens a browser for the OAuth consent flow.

Prerequisites:
  1. Go to https://console.cloud.google.com/
  2. Create a project → enable Gmail API
  3. OAuth consent screen → External → add your email as a test user
  4. Credentials → Create OAuth 2.0 Client ID (Desktop app) → download JSON
  5. Save it as credentials.json in this folder
"""

import os
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",   # read emails
    "https://www.googleapis.com/auth/gmail.send",        # send weekly report
]

CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent / "token.json"


def get_gmail_service():
    """Return an authenticated Gmail API service client.

    Flow:
    - If token.json exists and is valid, use it.
    - If expired but refreshable, auto-refresh and save.
    - If missing or invalid, open browser for OAuth consent, then save.
    """
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}.\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            print("Opening browser for Gmail OAuth consent...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            # port=0 lets the OS pick a free port automatically
            creds = flow.run_local_server(port=0, prompt="consent")

        TOKEN_FILE.write_text(creds.to_json())
        print(f"Token saved to {TOKEN_FILE}")

    return build("gmail", "v1", credentials=creds)


if __name__ == "__main__":
    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as: {profile['emailAddress']}")
    print(f"Total messages: {profile['messagesTotal']}")
