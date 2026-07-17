import os, sys
import time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash, check_password_hash
from app.BrightpathAI.models.users import User


def register_user(body: dict, db: Session):
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(400, "Email and password are required")

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(409, "User already exists")

    user = User(
        email=email,
        phone=body.get("phone", ""),
        first_name=body.get("first_name", ""),
        last_name=body.get("last_name", ""),
        role=body.get("role", "user"),
        designation=body.get("designation", ""),
        department=body.get("department", ""),
        education=body.get("education", ""),
        company_id=body.get("company_id"),
    )

    user.password = generate_password_hash(password)

    db.add(user)
    db.commit()
    db.refresh(user)

    return user


def login_user(body: dict, db: Session):
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(400, "Email and password are required")

    user = db.query(User).filter(User.email == email).first()
    if not user or not check_password_hash(user.password, password):
        raise HTTPException(401, "Invalid credentials")

    return user


def create_user_by_admin(body: dict, db: Session, admin_user: User):
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(400, "Email and password are required")

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(409, "User already exists")

    user = User(
        email=email,
        phone=body.get("phone", ""),
        first_name=body.get("first_name", ""),
        last_name=body.get("last_name", ""),
        role=body.get("role", "user"),
        designation=body.get("designation", ""),
        department=body.get("department", ""),
        education=body.get("education", ""),
        company_id=admin_user.company_id,  # inherit company
    )

    user.password = generate_password_hash(password)

    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user(user_id: int, db: Session):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    return user
