# app/routes/file_route.py
import os
import tempfile
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.model.file import FileReader
from app.schemas.file_schema import FileRead_sch
from app.generic.embedding import generate_embedding

import fitz  # PyMuPDF for PDF
from bs4 import BeautifulSoup

file_route = APIRouter(prefix="/api/files", tags=["files"])

# Dependency
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Helper: extract text
def extract_text_from_file(file_path: str, content_type: str) -> str:
    text_content = ""

    if content_type == "application/pdf":
        doc = fitz.open(file_path)
        for page in doc:
            text_content += page.get_text("text")
        doc.close()

    elif content_type in ["text/plain", "text/csv"]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text_content = f.read()

    elif content_type in ["text/html"]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
            text_content = soup.get_text(separator="\n")

    else:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    return text_content.strip()


@file_route.post("/read", response_model=FileRead_sch)
async def read_file(file: UploadFile = File(...), db: Session = Depends(db_session)):
    # Save to a temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        # Step 1: Extract text
        text_content = extract_text_from_file(tmp_path, file.content_type)

        if not text_content:
            raise HTTPException(status_code=400, detail="No readable text in file")

        # Step 2: Generate embedding
        vector = generate_embedding(text_content)

        # Step 3: Save to DB
        file_entry = FileReader(
            file_name=file.filename,
            file_contact=text_content,
            file_vector=vector
        )
        # db.add(file_entry)
        # db.commit()
        # db.refresh(file_entry)

        # Step 4: Return schema
        return FileRead_sch(
            file_name=file_entry.file_name,
            file_contact=file_entry.file_contact,
            file_vector=file_entry.file_vector
        )

    finally:
        os.remove(tmp_path)
