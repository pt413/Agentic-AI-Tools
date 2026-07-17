import os, json, tempfile
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from sqlalchemy.orm import Session
from googleapiclient.discovery import build
from app.model.email_credentials import GmailAccount

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "../credentials/credentials.json")


def _get_client_info():
    try:
        with open(CREDENTIALS_PATH, "r") as f:
            data = json.load(f)
            block = data.get("web") or data.get("installed")
            if not block:
                raise KeyError("credentials.json missing 'web' or 'installed' block")
            return block["client_id"], block["client_secret"]
    except Exception as e:
        raise RuntimeError(f"Could not load client secrets from {CREDENTIALS_PATH}: {e}")


def gmail_authenticate(email_address: str, db: Session, GmailAccount_model):

    creds = None
    account= None
    client_id, client_secret = _get_client_info()

    account = db.query(GmailAccount_model).filter(
        GmailAccount_model.email == email_address
    ).first()

    if account and account.refresh_token:
        creds_data = {
            "token": account.access_token,
            "refresh_token": account.refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": SCOPES
        }
        if account.token_expiry:
            creds_data["expiry"] = account.token_expiry

        creds = Credentials.from_authorized_user_info(creds_data, SCOPES)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)

    if creds and creds.valid:
        if not account:
            account = GmailAccount_model(
                email=email_address,
                refresh_token=creds.refresh_token or ""
            )
            db.add(account)

    account.access_token = creds.token
    account.refresh_token = creds.refresh_token
    account.token_expiry = creds.expiry.isoformat() if creds.expiry else None

    db.commit()

    os.environ["GOOGLE_API_PYTHON_CLIENT_CACHE_PATH"] = tempfile.gettempdir()

    return build("gmail", "v1", credentials=creds)
