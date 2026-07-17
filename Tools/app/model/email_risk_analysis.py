from datetime import datetime
from sqlalchemy import Column, Integer, Text, Float, TIMESTAMP, ForeignKey
from app.db.database import Base
from sqlalchemy import Boolean



class EmailRiskAnalysis(Base):
    __tablename__ = "email_risk_analysis"

    id = Column(Integer, primary_key=True)

    rag_embedding_id = Column(Integer, nullable=False)
    source_id = Column(Text, nullable=False)

    risk_label = Column(Text, nullable=False)
    risk_score = Column(Float, nullable=False)

    bart_model = Column(Text, nullable=False)
    similarity_score = Column(Float, nullable=True)

    validated_by_llm = Column(Boolean, default=False)
    llm_verdict = Column(Text, nullable=True)

    processed_at = Column(TIMESTAMP, default=datetime.utcnow)
