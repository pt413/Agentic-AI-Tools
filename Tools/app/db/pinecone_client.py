import os
from dotenv import load_dotenv
from pinecone import Pinecone,ServerlessSpec
from sentence_transformers import SentenceTransformer
from app.db.database import SessionLocal
from app.model.emails import Email

load_dotenv()

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_ENV = "us-east-1-aws"
INDEX = "emails"
BATCH_SIZE = 50

pc = Pinecone(api_key=PINECONE_API_KEY, environment=PINECONE_ENV)

if INDEX not in [i.name for i in pc.list_indexes()]:
    pc.create_index(
        name=INDEX,
        dimension=384,
        metric="cosine",
        spec=ServerlessSpec(
            cloud="aws",
            region="us-east-1"
        )
    )

index = pc.Index(INDEX)
model = SentenceTransformer("all-MiniLM-L6-v2")
db = SessionLocal()
emails = db.query(Email).all()
print(f"Fetched {len(emails)} emails from Postgres")

vectors_batch = []

for email in emails:
    content = f"""
        Email Subject:
        {email.subject or ''}

        Email Body:
        {email.body or ''}
        """
    embedding = model.encode(content).tolist()

    metadata = {
        "subject": email.subject,
        "sender": email.sender,
        "receiver": email.receiver,
        "date": str(email.date),
        "msgid": email.msgid,
        "thread_id": email.thread_id
    }

    vectors_batch.append({
        "id": str(email.id),
        "values": embedding,
        "metadata": metadata
    })

    if len(vectors_batch) >= BATCH_SIZE:
        index.upsert(vectors=vectors_batch)
        vectors_batch = []

if vectors_batch:
    index.upsert(vectors=vectors_batch)

print("All emails successfully indexed to Pinecone!")
db.close()
