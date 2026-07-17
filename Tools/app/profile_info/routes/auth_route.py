from fastapi import APIRouter, Depends, Body, HTTPException
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.BrightpathAI.utils.auth_dependency import get_current_user
# from app.BrightpathAI.services import auth_service
from app.BrightpathAI.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
def register_user(body: dict = Body(...), db: Session = Depends(get_db)):
    return auth_service.register_user(body, db)


@router.post("/login")
def login_user(body: dict = Body(...), db: Session = Depends(get_db)):
    return auth_service.login_user(body, db)


@router.post("/create-user")
def create_user_by_admin(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    if current_user.role != "admin":
        raise HTTPException(403, "Not authorized")

    return auth_service.create_user_by_admin(body, db, current_user)


@router.get("/user/{user_id}")
def get_user(user_id: int, db: Session = Depends(get_db)):
    return auth_service.get_user(user_id, db)
