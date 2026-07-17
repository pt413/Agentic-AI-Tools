# import os
# import pickle
# from googleapiclient.discovery import build
# from pathlib import Path

# BASE_DIR = Path(__file__).resolve().parents[3]
# TOKEN_PATH = BASE_DIR / "credentials" / "token.pickle"
# SCOPES = ['https://www.googleapis.com/auth/contacts']

# def get_google_service():
#     with open(TOKEN_PATH, 'rb') as token:
#         creds = pickle.load(token)
#     return build('people', 'v1', credentials=creds)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]
TOKEN_PATH = BASE_DIR / "credentials" / "token.pickle"
SCOPES = ['https://www.googleapis.com/auth/contacts']

def get_google_service(token_path):
    creds = None
    if os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)

    if not creds:
        raise Exception("No token found. Run OAuth flow again.")

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, 'wb') as token:
                pickle.dump(creds, token)
        except Exception:
            raise Exception("Token expired and refresh failed.")
    elif creds.expired:
        raise Exception("Token expired. No refresh token available.")

    return build('people', 'v1', credentials=creds)