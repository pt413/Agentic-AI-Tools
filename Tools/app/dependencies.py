# from fastapi import Depends, HTTPException, Header
# from sqlalchemy.orm import Session
# from app.db.database import get_db
# from app.model.user import User

# def get_current_user(x_user_id: int = Header(...), db: Session = Depends(get_db)):
#     """
#     Fetch the current user based on a header 'X-User-Id'.
#     Note: Only for testing/dev purposes. Not secure for production.
#     """
#     user = db.query(User).filter(User.id == x_user_id).first()
#     if not user:
#         raise HTTPException(status_code=404, detail="User not found")
#     return user
