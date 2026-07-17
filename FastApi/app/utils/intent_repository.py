from sqlalchemy.orm import Session
from app.model.intent import Intent

def get_all_intents(db: Session):
    return db.query(Intent).all()

def get_intent_by_name(db: Session, name: str):
    return db.query(Intent).filter(Intent.name == name).first()

def store_intent(db: Session, name: str, example_query: str, embedding: list):
    """Insert a new intent if it doesn't already exist."""
    existing = get_intent_by_name(db, name)
    if existing:
        return existing

    new_intent = Intent(
        name=name,
        example_query=example_query,
        embedding=embedding
    )
    db.add(new_intent)
    db.commit()
    db.refresh(new_intent)
    return new_intent
