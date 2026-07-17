# from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func
# from sqlalchemy.dialects.postgresql import JSONB
# from app.db.database import Base
# from sqlalchemy.orm import relationship
# from app.model.user import User

# class UserChatMessage(Base):
#     __tablename__ = "user_chat_messages"

#     id = Column(Integer, primary_key=True, index=True)
#     user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
#     chat_session_id = Column(String, nullable=False)
#     question = Column(Text)
#     ans = Column(Text)
#     query_data = Column(JSONB, default=None) 
#     created_at = Column(DateTime(timezone=True), server_default=func.now())

#     users = relationship("User", back_populates="user_chat_messages")
