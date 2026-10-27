"""Revocable, hashed opaque sessions. No signing secret is embedded in the app."""
import hashlib
import secrets
from threading import BoundedSemaphore
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuthRateLimit, AuthSession, UsedRefreshToken, User, UsersParams
from app.schemas import UserCreate, UserLogin, UserResponse
from app.models import UsersRealSleepData, UsersTargetWakingPeriod, UsersRealTimeIntake

router = APIRouter(tags=["Account"])
bearer = HTTPBearer(auto_error=False)
passwords = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto",
                         argon2__memory_cost=19456, argon2__time_cost=2, argon2__parallelism=1)
_kdf_slots = BoundedSemaphore(2)
ACCESS_LIFETIME = timedelta(minutes=15)
SESSION_LIFETIME = timedelta(days=30)
_DUMMY_HASH = passwords.hash(secrets.token_urlsafe(24))


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def unauthorized():
    return HTTPException(401, "session_expired", headers={"WWW-Authenticate": "Bearer"})


def check_new_password(value):
    if not value.strip() or len(value.encode("utf-8")) > 72:
        raise HTTPException(422, "password_must_not_be_blank_max_72_bytes")


def cleanup_expired_sessions(db):
    timestamp = now()
    expired = select(AuthSession.id).where(AuthSession.refresh_expires_at <= timestamp)
    db.query(UsedRefreshToken).filter(or_(UsedRefreshToken.expires_at <= timestamp,
        UsedRefreshToken.session_id.in_(expired))).delete(synchronize_session=False)
    db.query(AuthSession).filter(AuthSession.refresh_expires_at <= timestamp).delete(synchronize_session=False)
    db.query(AuthRateLimit).filter(AuthRateLimit.expires_at <= timestamp).delete(synchronize_session=False)
    db.commit()


def consume_limit(db, category, identity, limit, seconds=900):
    """DB-backed limits survive process restarts and apply across workers."""
    key = digest(category + "|" + identity)
    timestamp = now()
    row = db.query(AuthRateLimit).filter_by(bucket=key).with_for_update().first()
    if row is None:
        try:
            with db.begin_nested():
                db.add(AuthRateLimit(bucket=key, count=0, expires_at=timestamp + timedelta(seconds=seconds)))
                db.flush()
        except IntegrityError:
            pass
        row = db.query(AuthRateLimit).filter_by(bucket=key).with_for_update().one()
    if utc(row.expires_at) <= timestamp:
        row.count = 0
        row.expires_at = timestamp + timedelta(seconds=seconds)
    if row.count >= limit:
        retry = max(1, int((utc(row.expires_at) - timestamp).total_seconds()))
        db.commit()
        raise HTTPException(429, "too_many_attempts", headers={"Retry-After": str(retry)})
    row.count += 1
    db.commit()


def safe_user(user):
    return {"user_id": str(user.user_id), "name": user.name or "", "email": user.email,
            "last_login": user.last_login}


def issue_pair(db, session, user):
    access, refresh = "wma_" + secrets.token_urlsafe(32), "wmr_" + secrets.token_urlsafe(32)
    session.access_hash, session.refresh_hash = digest(access), digest(refresh)
    session.access_expires_at = now() + ACCESS_LIFETIME
    db.add(session)
    db.commit()
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
            "expires_in": int(ACCESS_LIFETIME.total_seconds()),
            "refresh_expires_at": session.refresh_expires_at,
            "password_change_required": session.password_change_required,
            "user": safe_user(user)}


def get_current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
                     db: Session = Depends(get_db)):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()
    token = credentials.credentials
    if not token.startswith("wma_") or not 40 <= len(token) <= 100:
        raise unauthorized()
    session = db.query(AuthSession).filter_by(access_hash=digest(token)).first()
    if (session is None or session.revoked_at is not None or utc(session.access_expires_at) <= now()
            or utc(session.refresh_expires_at) <= now()):
        raise unauthorized()
    user = db.query(User).filter_by(user_id=session.user_id).first()
    if user is None:
        raise unauthorized()
    request.state.auth_session = session
    request.state.auth_user = user
    return user


def scoped_user_id(user_id: UUID | None = Query(None), user: User = Depends(get_current_user)):
    if user_id is not None and user_id != user.user_id:
        raise HTTPException(403, "not_permitted")
    return user.user_id


async def enforce_data_owner(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if request.state.auth_session.password_change_required:
        raise HTTPException(403, "password_change_required")
    supplied = request.query_params.getlist("user_id")
    if any(value != str(user.user_id) for value in supplied):
        raise HTTPException(403, "not_permitted")
    if request.path_params:
        model = {
            "/users_sleep/{entry_id}": UsersRealSleepData,
            "/users_wake/{entry_id}": UsersTargetWakingPeriod,
            "/users_intake/{entry_id}": UsersRealTimeIntake,
        }.get(request.scope["route"].path)
        if model is None:
            raise HTTPException(403, "not_permitted")
        entry_id = request.path_params.get("entry_id")
        if not str(entry_id).isdigit() or len(str(entry_id)) > 10 or int(entry_id) > 2147483647:
            raise HTTPException(404, "record_not_found")
        if db.query(model.id).filter(model.id == int(entry_id), model.user_id == user.user_id).first() is None:
            raise HTTPException(404, "record_not_found")
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            body = await request.json()
        except ValueError:
            return  # FastAPI will produce its normal validation response.
        if isinstance(body, dict):
            owners = [body] if "user_id" in body else []
            records = body.get("records")
            if isinstance(records, list):
                owners.extend(records)
            for item in owners:
                if isinstance(item, dict) and str(item.get("user_id")) != str(user.user_id):
                    raise HTTPException(403, "not_permitted")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=40, max_length=100)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=72)


@router.post("/users/", response_model=UserResponse)
def register(data: UserCreate, request: Request, db: Session = Depends(get_db)):
    consume_limit(db, "register-ip", request.client.host if request.client else "unknown", 20)
    check_new_password(data.password)
    email = str(data.email).strip().lower()
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "email_already_registered")
    with _kdf_slots:
        hashed = passwords.hash(data.password)
    user = User(name=data.name.strip(), email=email, hashed_password=hashed)
    try:
        db.add(user)
        db.flush()
        db.add(UsersParams(user_id=user.user_id))
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "email_already_registered")
    return user


@router.post("/login/")
def login(data: UserLogin, request: Request, db: Session = Depends(get_db)):
    email = str(data.email).strip().lower()
    consume_limit(db, "login-email", email, 10)
    consume_limit(db, "login-ip", request.client.host if request.client else "unknown", 100)
    user = db.query(User).filter(func.lower(User.email) == email).first()
    password = data.password
    valid = False
    if len(password.encode("utf-8")) <= 72:
        with _kdf_slots:
            valid = passwords.verify(password, user.hashed_password if user else _DUMMY_HASH)
    if user is None or not valid:
        raise HTTPException(401, "invalid_credentials", headers={"WWW-Authenticate": "Bearer"})
    if passwords.needs_update(user.hashed_password):
        with _kdf_slots:
            user.hashed_password = passwords.hash(password)
    user.last_login = now()
    session = AuthSession(user_id=user.user_id, created_at=now(),
                          refresh_expires_at=now() + SESSION_LIFETIME,
                          password_change_required=not password.strip())
    return issue_pair(db, session, user)


@router.post("/auth/refresh")
def refresh(data: RefreshRequest, db: Session = Depends(get_db)):
    hashed = digest(data.refresh_token)
    session = db.query(AuthSession).filter_by(refresh_hash=hashed).with_for_update().first()
    if session is None:
        used = db.query(UsedRefreshToken).filter_by(token_hash=hashed).first()
        if used is not None:
            db.query(AuthSession).filter_by(id=used.session_id).update({"revoked_at": now()})
            db.commit()
        raise unauthorized()
    if session.revoked_at is not None or utc(session.refresh_expires_at) <= now():
        raise unauthorized()
    user = db.query(User).filter_by(user_id=session.user_id).one()
    db.add(UsedRefreshToken(token_hash=hashed, session_id=session.id, expires_at=session.refresh_expires_at))
    return issue_pair(db, session, user)


@router.get("/auth/me")
def me(request: Request, user: User = Depends(get_current_user)):
    return {**safe_user(user), "password_change_required": request.state.auth_session.password_change_required}


@router.post("/auth/logout")
def logout(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    request.state.auth_session.revoked_at = now()
    db.commit()
    return {"status": "success"}


@router.post("/auth/change_password")
def change_password(data: PasswordChange, request: Request, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    consume_limit(db, "change-password", str(user.user_id), 5)
    check_new_password(data.new_password)
    with _kdf_slots:
        valid = len(data.current_password.encode("utf-8")) <= 72 and passwords.verify(data.current_password, user.hashed_password)
    if not valid:
        raise HTTPException(400, "invalid_current_password")
    if data.current_password == data.new_password:
        raise HTTPException(400, "choose_a_different_password")
    with _kdf_slots:
        user.hashed_password = passwords.hash(data.new_password)
    current = request.state.auth_session
    db.query(AuthSession).filter(AuthSession.user_id == user.user_id, AuthSession.id != current.id).update(
        {"revoked_at": now()}, synchronize_session=False)
    current.password_change_required = False
    db.add(UsedRefreshToken(token_hash=current.refresh_hash, session_id=current.id,
                            expires_at=current.refresh_expires_at))
    return issue_pair(db, current, user)
