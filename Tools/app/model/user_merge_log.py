# # app/model/user_merge_log.py
# from sqlalchemy import Column, Integer, Text, TIMESTAMP, ForeignKey, text
# from sqlalchemy.dialects.postgresql import JSONB
# from app.db.database import Base  # ← USE SHARED BASE (not declarative_base())

# class UserMergeLog(Base):
#     __tablename__ = "user_merge_log"
    
#     id = Column(Integer, primary_key=True)
#     master_id = Column(Integer, ForeignKey("user_data_merge_test.u_id"), nullable=False)
#     merged_id = Column(Integer, ForeignKey("user_data_merge_test.u_id", ondelete="SET NULL"), nullable=False)
#     merged_at = Column(TIMESTAMP, server_default=text('now()'), nullable=False)
#     fields_merged = Column(JSONB)
#     activities_reassigned = Column(Integer)
#     reason = Column(Text)
#     lead_id = Column(Text)
#     notes = Column(Text)

# # app/model/user_merge_log.py
# from sqlalchemy import Column, Integer, Text, TIMESTAMP, ForeignKey, text
# from sqlalchemy.dialects.postgresql import JSONB
# from app.db.database import Base  # ← USE SHARED BASE (not declarative_base())

# class UserMergeLog(Base):
#     __tablename__ = "user_merge_log"
    
#     id = Column(Integer, primary_key=True)
#     master_id = Column(Integer, ForeignKey("user_data_merge_test.u_id"), nullable=False)
#     merged_id = Column(Integer, ForeignKey("user_data_merge_test.u_id", ondelete="SET NULL"), nullable=False)
#     merged_at = Column(TIMESTAMP, server_default=text('now()'), nullable=False)
#     fields_merged = Column(JSONB)
#     activities_reassigned = Column(Integer)
#     reason = Column(Text)
#     lead_id = Column(Text)
#     notes = Column(Text)

# app/model/user_merge_log.py
from sqlalchemy import Column, Integer, Text, TIMESTAMP, ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB
from app.db.database import Base  # ← USE SHARED BASE

class UserMergeLog(Base):
    __tablename__ = "user_merge_log"
    
    id = Column(Integer, primary_key=True)
    master_id = Column(Integer, ForeignKey("user_data_merge_test.u_id"), nullable=False)
    merged_id = Column(Integer, nullable=True)  
    merged_at = Column(TIMESTAMP, server_default=text('now()'), nullable=False)
    fields_merged = Column(JSONB)
    activities_reassigned = Column(Integer)
    reason = Column(Text)
    lead_id = Column(Text)
    notes = Column(Text)