from datetime import datetime
from sqlalchemy import Column, Integer, Text, Float, TIMESTAMP, Boolean
from app.db.database import Base


class EmailRiskAnalysisV2(Base):
    __tablename__ = "email_risk_analysis_v2"

    id = Column(Integer, primary_key=True)

    rag_embedding_id = Column(Integer, nullable=False)
    source_id = Column(Text, nullable=False)

    # Final predicted label (can be normal or risk label)
    risk_label = Column(Text, nullable=False)

    # BGE similarity score of best matching label
    similarity_score = Column(Float, nullable=True)

    # Whether LLM was triggered
    llm_called = Column(Boolean, default=False)

    # Whether LLM call succeeded
    llm_success = Column(Boolean, default=False)

    # Reason returned by LLM or system stage explanation
    llm_reason = Column(Text, nullable=True)

    # Which stage made final decision
    # e.g. below_0.48, high_conf_embedding, llm_validation, keyword_llm
    decision_stage = Column(Text, nullable=False)

    processed_at = Column(TIMESTAMP, default=datetime.utcnow)