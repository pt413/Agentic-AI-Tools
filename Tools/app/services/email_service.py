import pytz
from datetime import datetime, timedelta, timezone as dt_timezone
from sqlalchemy.orm import Session
from typing import List, Dict
from app.model.emails import Email
from app.model.email_credentials import GmailAccount
from app.utils.email_auth import gmail_authenticate
from app.generic.email_fetcher import fetch_emails
import logging

logger = logging.getLogger(__name__)


class EmailService:

    def __init__(self, db: Session):
        self.db = db

    def fetch_emails_for_address(self, email_address: str):

        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)

        after = int((now_ist - timedelta(days=1)).timestamp())
        before = int(now_ist.timestamp())

        query = f"after:{after} before:{before} in:anywhere"
        
        logger.info(f"🔐 Authenticated Gmail: {email_address}")

        service = gmail_authenticate(
            email_address,
            db=self.db,
            GmailAccount_model=GmailAccount
        )

        result = fetch_emails(
            service,
            query=query,
            db=self.db,
            EmailModel=Email
        )

        fetched = result.get("fetched", 0)
        stored = result.get("stored", 0)
        skipped = result.get("skipped", 0)

        logger.info(f" Gmail returned: {fetched}")
        logger.info(f" Stored in DB: {stored}")
        logger.info(f"⏭ Skipped duplicates: {skipped}")

        return result
    
    def fetch_all_accounts(self) -> List[Dict]:
        accounts = self.db.query(GmailAccount).all()

        results = []

        for acc in accounts:
            try:
                res = self.fetch_emails_for_address(acc.email)
                results.append({
                    "email": acc.email,
                    "fetched": res.get("fetched", 0),
                    "stored": res.get("stored", 0),
                    "skipped": res.get("skipped", 0)
                })

            except Exception as e:
                logger.error(f"Error fetching emails for {acc.email}: {str(e)}")
                results.append({
                    "email": acc.email,
                    "error": str(e)
                })

        return results
