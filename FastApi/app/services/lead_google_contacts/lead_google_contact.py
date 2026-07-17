from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import pickle

ACCOUNT_NAME = input("Enter account name (e.g. lead / whatsapp): ").strip()
SCOPES = ['https://www.googleapis.com/auth/contacts']

flow = InstalledAppFlow.from_client_secrets_file(
    'credentials/credentials.json', SCOPES
)
# creds = flow.run_local_server(port=0)
creds = flow.run_local_server(port=0,access_type="offline",prompt="consent")

token_path = f'credentials/token_{ACCOUNT_NAME}.pickle'

with open(token_path, 'wb') as token:
    pickle.dump(creds, token)

print(f"✅ Token saved at {token_path}")

print("✅ Auth complete")