# app/schemas/file_schema.py
from pydantic import BaseModel
from typing import Optional, List

class FileRead_sch(BaseModel):
    file_name: str
    file_contact: str
    file_vector: Optional[List[float]] = None

    class Config:
        orm_mode = True
