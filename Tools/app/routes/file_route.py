import os
import pandas as pd
import numpy as np
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from sqlalchemy.orm import Session
from app.model.file import UploadedFile
from app.schemas.file_schema import UploadedFileResponse
from app.db.database import SessionLocal

router = APIRouter(prefix="/api/files", tags=["files"])

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/", response_model=UploadedFileResponse)
async def create_file(
    file: UploadFile = File(...),
    userId: str = Form("unknown"),
    db: Session = Depends(get_db)
):
    ext = os.path.splitext(file.filename)[1].lower()

    try:
        if ext == ".csv":
            df = pd.read_csv(file.file, encoding="utf-8")
        elif ext in [".xls", ".xlsx"]:
            df = pd.read_excel(file.file)
        elif ext == ".json":
            df = pd.read_json(file.file)
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type")

        data = df.to_dict(orient="records")

        # Replace NaN with None
        for row in data:
            for k, v in row.items():
                if isinstance(v, float) and np.isnan(v):
                    row[k] = None

        # Limit records
        if len(data) > 10000:
            raise HTTPException(status_code=400, detail="Too many records")

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")

    uploaded_file = UploadedFile(
        filename=file.filename,
        content_type=file.content_type,
        user_id=userId,
        data=data
    )
    db.add(uploaded_file)
    db.commit()
    db.refresh(uploaded_file)

    return uploaded_file


@router.get("/", response_model=list[UploadedFileResponse])
def list_files(db: Session = Depends(get_db)):
    return db.query(UploadedFile).all()


@router.get("/{file_id}", response_model=UploadedFileResponse)
def get_file(file_id: int, db: Session = Depends(get_db), full: bool = False):
    file = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    if not full:
        file.data = file.data[:5] if file.data else []

    return file


@router.delete("/{file_id}")
def delete_file(file_id: int, db: Session = Depends(get_db)):
    file = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    db.delete(file)
    db.commit()
    return {"success": True, "message": "File deleted"}
