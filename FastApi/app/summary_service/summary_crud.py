from sqlalchemy.orm import Session
from db import models, schemas

def create_summary(db: Session, summary: schemas.SummaryCreate):
    db_summary = models.Summary(**summary.dict())
    db.add(db_summary)
    db.commit()
    db.refresh(db_summary)
    return db_summary

def get_summary(db: Session, summary_id: int):
    return db.query(models.Summary).filter(models.Summary.id == summary_id).first()
