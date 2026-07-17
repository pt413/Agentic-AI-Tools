from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

from sqlalchemy.sql.operators import op

# -------------------------------
# Input schema for creating a user
# -------------------------------
class UserCreate(BaseModel):
    email: EmailStr
    phone: Optional[str] = ""
    password: str
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    role: Optional[str] = "user"          # default new users to "user"
    designation: Optional[str] = ""
    department: Optional[str] = ""        # new field
    education: Optional[str] = ""         # new field
    company_id: Optional[int] = None    # new field (organization link)


# -------------------------------
# Input schema for login
# -------------------------------
class UserLogin(BaseModel):
    email: EmailStr
    password: str


# -------------------------------
# Response schema
# -------------------------------
class UserResponse(BaseModel):
    id: int
    email: str
    phone: str
    first_name: str
    last_name: str
    role: str
    designation: str
    department: str
    education: str
    company_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True
