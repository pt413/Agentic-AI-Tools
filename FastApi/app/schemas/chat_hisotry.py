from pydantic import BaseModel
from datetime import datetime

class UserChatMessageCreate(BaseModel):
    user_id: int
    chat_session_id: str
    question: str
    ans: str

class UserChatMessageResponse(UserChatMessageCreate):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True
