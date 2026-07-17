# from fastapi import APIRouter, Depends, HTTPException, status
# from sqlalchemy.orm import Session
# from app.controllers import auth_controller
# from app.schemas.user_schema import UserCreate, UserLogin, UserResponse
# from app.db.database import get_db
# from app.dependencies import get_current_user

# router = APIRouter(prefix="/api/auth", tags=["auth"])

# @router.post("/register", response_model=UserResponse)
# async def register_user(payload: UserCreate, db: Session = Depends(get_db)):
#     user = auth_controller.register_user(payload, db)
#     return user

# @router.post("/login", response_model=UserResponse)
# async def login_user(payload: UserLogin, db: Session = Depends(get_db)):
#     print("user credentials " , payload)
#     user = auth_controller.login_user(payload, db)
#     return user

# # Admin creates user under their company
# @router.post("/create-user", response_model=UserResponse)
# def create_user_by_admin(
#     payload: UserCreate,
#     db: Session = Depends(get_db),
#     current_user = Depends(get_current_user)
# ):
#     if current_user.role != "admin":
#         raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    
#     user = auth_controller.create_user_by_admin(payload, db, current_user)
#     return user

# @router.get("/user/{user_id}", response_model=UserResponse)
# def get_user(user_id: int, db: Session = Depends(get_db)):
#     user = auth_controller.get_user(user_id, db)    
#     return user
    