from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.models.database import User
from backend.services.audit_logger import AuditLogger

# bcrypt only looks at the first 72 bytes, newer versions refuse longer input, so cut it here
# (the hashes are the usual $2b$ format, so ones made earlier still verify)
def _pw(password: str) -> bytes:
    return password.encode("utf-8")[:72]


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_pw(plain), hashed.encode("utf-8"))
    except ValueError:   # not a valid bcrypt hash
        return False


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(_pw(password), bcrypt.gensalt()).decode("utf-8")


def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).first()


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    user = get_user_by_username(db, username)
    if not user:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.JWT_EXPIRY_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"}
        )


def create_user(
    db: Session,
    username: str,
    email: str,
    password: str,
    is_admin: bool = False
) -> User:
    hashed = get_password_hash(password)
    user = User(
        username=username,
        email=email,
        hashed_password=hashed,
        is_admin=is_admin,
        is_active=True,
        created_at=datetime.utcnow()
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_default_admin(db: Session) -> User:
    """Create default admin user if not exists"""
    admin = get_user_by_username(db, settings.DEFAULT_ADMIN_USERNAME)
    if not admin:
        admin = create_user(
            db,
            username=settings.DEFAULT_ADMIN_USERNAME,
            email=settings.DEFAULT_ADMIN_EMAIL,
            password=settings.DEFAULT_ADMIN_PASSWORD,
            is_admin=True
        )
    return admin