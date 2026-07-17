# from fastapi import Depends, HTTPException, status
# from sqlalchemy.orm import Session
# from app.model.user import User
# from app.schemas.user_schema import UserCreate, UserLogin, UserResponse
# from app.db.database import get_db
# from werkzeug.security import generate_password_hash, check_password_hash

# def register_user(payload: UserCreate, db: Session = Depends(get_db)):
#     # Check if user already exists
#     existing = db.query(User).filter(User.email == payload.email).first()
#     if existing:
#         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

#     user = User(
#         email=payload.email,
#         phone=payload.phone,
#         first_name=payload.first_name,
#         last_name=payload.last_name,
#         role=payload.role if payload.role else "user",
#         designation=payload.designation if payload.designation else "",
#         department=payload.department if payload.department else "",
#         education=payload.education if payload.education else "",
#         company_id=payload.company_id
#     )
#     user.password = generate_password_hash(payload.password)

#     db.add(user)
#     db.commit()
#     db.refresh(user)

#     return user


# def create_user_by_admin(payload: UserCreate, db: Session, admin_user: User):
#     """
#     Creates a new user under the same company as the admin.
#     """
#     # Check if user already exists
#     existing = db.query(User).filter(User.email == payload.email).first()
#     if existing:
#         raise HTTPException(status_code=409, detail="User already exists")

#     user = User(
#         email=payload.email,
#         phone=payload.phone or "",
#         first_name=payload.first_name or "",
#         last_name=payload.last_name or "",
#         role=payload.role or "user",
#         designation=payload.designation or "",
#         department=payload.department or "",
#         education=payload.education or "",
#         company_id=admin_user.company_id  # automatically inherit from admin
#     )

#     user.password = generate_password_hash(payload.password)

#     db.add(user)
#     db.commit()
#     db.refresh(user)
#     return user




# def login_user(payload: UserLogin, db: Session = Depends(get_db)):
#     user = db.query(User).filter(User.email == payload.email).first()
#     if not user or not check_password_hash(user.password, payload.password):
#         raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
#     return user


# def get_user(user_id: int, db: Session = Depends(get_db)):
#     user = db.query(User).filter(User.id == user_id).first()
#     if not user:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
#     return user
    
