# app/setup/intent_loader.py
from sqlalchemy.orm import Session
from app.model.intent import Intent
from app.generic.embedding import intent_generate_embedding

# Predefined intents (minimal and realistic)
PREDEFINED_INTENTS = [
    ("check_booking_details", "Show me booking details for 52390"),
    ("cancel_booking", "Cancel my booking number 8123"),
    ("check_payment_status", "What is the payment status for my invoice?"),
    ("refund_request", "I want a refund for my last payment"),
    ("lead_status", "Check status of my customer lead"),
    ("faq_query", "What is your refund policy?"),
    ("follow_up", "About that last booking I mentioned"),
]

async def load_predefined_intents(db: Session):
    """Insert predefined intents into the DB if missing."""
    existing_intents = {i.name for i in db.query(Intent.name).all()}

    for name, example in PREDEFINED_INTENTS:
        if name not in existing_intents:
            embedding = await intent_generate_embedding(example)
            new_intent = Intent(
                name=name,
                example_query=example,
                embedding=embedding,
            )
            db.add(new_intent)
            print(f"✅ Added intent: {name}")

    db.commit() 
    print("✨ Intent table initialized successfully.")
