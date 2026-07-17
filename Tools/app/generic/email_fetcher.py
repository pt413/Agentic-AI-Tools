import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from app.generic.emails_body_utils import extract_email_body

def extract_email(raw):
    match = re.search(r'[\w\.\+\-]+@[\w\.\-]+\.\w+', raw)
    return match.group(0) if match else raw.strip()

def fetch_emails(service, query="", db=None, EmailModel=None):

    emails = []

    db_objects = []
    existing = set()

    user_email = service.users().getProfile(userId="me").execute()["emailAddress"]

    page_token = None
    while True:
        response = service.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=page_token
        ).execute()

        for msg in response.get("messages", []):
            msg_data = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()

            payload = msg_data.get("payload", {})
            headers = payload.get("headers", [])

            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
            raw_sender = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
            raw_receiver = next((h['value'] for h in headers if h['name'] == 'To'), '(Unknown Receiver)')

            sender = extract_email(raw_sender.replace('=gmail.com', '@gmail.com'))
            receiver = extract_email(raw_receiver.replace('=gmail.com', '@gmail.com'))

            date_header = next((h['value'] for h in headers if h['name'] == 'Date'), None)
            if date_header:
                try:
                    date_obj = parsedate_to_datetime(date_header)
                except:
                    internal_ts = int(msg_data.get('internalDate', 0)) / 1000
                    date_obj = datetime.utcfromtimestamp(internal_ts)
            else:
                internal_ts = int(msg_data.get('internalDate', 0)) / 1000
                date_obj = datetime.utcfromtimestamp(internal_ts)

            formatted_date = date_obj.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

            body = extract_email_body(payload)
            direction = "outgoing" if user_email.lower() in sender.lower() else "incoming"
            # query_vector = embedder.encode(body).tolist()
            # subject_vector=embedder.encode(subject).tolist()

            emails.append({
                "subject": subject,
                "sender": sender,
                "receiver": receiver,
                "date": formatted_date,
                "date_obj": date_obj,   
                "direction": direction,
                "body": body,
                "snippet": msg_data.get('snippet', '')[:200],
                "msgid": msg_data["id"],
                "thread_id": msg_data.get("threadId"),
                # "embedding": query_vector
                # "subject_emb": subject_vector
            })

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if db and EmailModel:

        new_msg_ids = [e["msgid"] for e in emails]

        existing = {
            r[0] for r in db.query(EmailModel.msgid)
                           .filter(EmailModel.msgid.in_(new_msg_ids)).all()
        }

        unique = [e for e in emails if e["msgid"] not in existing]

        db_objects = [
            EmailModel(
                subject=e["subject"],
                sender=e["sender"],
                receiver=e["receiver"],
                direction=e["direction"],
                date=e["date_obj"],
                body=e["body"],
                msgid=e["msgid"],
                thread_id=e["thread_id"],
                # embedding=e['embedding'],
                # subject_emb=e['subject_emb'],
                last_updated=datetime.now()
            )
            for e in unique
        ]

        if db_objects:
            db.add_all(db_objects)
            db.commit()

    return {
        "fetched": len(emails),
        "stored": len(db_objects),
        "skipped": len(existing),
        "emails": emails
    }