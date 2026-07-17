# from sqlalchemy import Column, Integer, String, DateTime, func
# from sqlalchemy.orm import validates
# from werkzeug.security import generate_password_hash, check_password_hash
# from app.db.database import Base
# from sqlalchemy import ForeignKey
# from sqlalchemy.orm import relationship
# from app.model.organization import Organization
# from sqlalchemy.dialects.postgresql import JSONB

# class User(Base):
#     __tablename__ = "users"

#     id = Column(Integer, primary_key=True, index=True)
#     email = Column(String, unique=True, nullable=False, index=True)
#     phone = Column(String, default="")
#     password = Column(String, nullable=False)
#     first_name = Column(String, default="")
#     last_name = Column(String, default="")
#     role = Column(String, default="user", nullable=False)
#     designation = Column(String, default="", nullable=False)
#     department = Column(String, default="", nullable=False)
#     education = Column(String, default="")
#     company_id = Column(Integer, ForeignKey("organization.id"))
#     created_at = Column(DateTime(timezone=True), server_default=func.now())

#     # Relationship to organization
#     organization = relationship("Organization", back_populates="users")

#     # Minimal addition: link to chat messages
#     user_chat_messages = relationship("UserChatMessage", back_populates="users", cascade="all, delete-orphan")


#     def set_password(self, password: str):
#         self.password = generate_password_hash(password)

#     def verify_password(self, password: str) -> bool:
#         return check_password_hash(self.password, password)

#     @validates("email")
#     def validate_email(self, key, email):
#         if not email or "@" not in email:
#             raise ValueError("Invalid email")
#         return email
