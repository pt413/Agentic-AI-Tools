from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


# -----------------------------
# Base Message
# -----------------------------
class MessageBase(BaseModel):
    message_id: str
    direction: str
    admin_number: str
    cx_number: str
    content: Optional[str] = None
    clean_content: Optional[str] = None
    timestamp: Optional[datetime] = None
    # media: Optional[str] = None
    device: Optional[str] = None
    issent: Optional[bool] = False
    isread: Optional[bool] = False


# -----------------------------
# Response Schemas
# -----------------------------
class MessageResponse(MessageBase):
    """Represents a single message record returned from /messages"""
    timestamp: Optional[str]  # ISO string (as you serialize in routes)

    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class MessagesListResponse(BaseModel):
    """For GET /messages"""
    success: bool
    count: int
    data: List[MessageResponse]


class ConversationResponse(BaseModel):
    """Represents a conversation summary in /conversations"""
    contactId: str
    lastMessage: Optional[str] = None
    lastMessageAt: Optional[str] = None
    unreadCount: int


class ConversationsListResponse(BaseModel):
    """For GET /conversations"""
    __root__: List[ConversationResponse]
