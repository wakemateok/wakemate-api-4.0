"""Google ID tokens identify accounts; WakeMate still issues its own revocable sessions."""
import os
import re

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    SESSION_LIFETIME, _kdf_slots, consume_limit, get_current_user,
    has_password_login, issue_pair, now, passwords,
)
from app.database import get_db
from app.models import AuthSession, GoogleIdentity, User, UsersParams

router = APIRouter(prefix="/auth/google", tags=["Account"])
_CLIENT_ID = re.compile(r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com")
_email = TypeAdapter(EmailStr)


def configured_client_id():
    value = os.getenv("WAKEMATE_GOOGLE_WEB_CLIENT_ID", "").strip()
    return value if _CLIENT_ID.fullmatch(value) else None


class GoogleToken(BaseModel):
    id_token: str = Field(min_length=20, max_length=8192)


class GoogleLink(GoogleToken):
    current_password: str = Field(min_length=1, max_length=128)


@router.get("/config")
def configuration():
    client_id = configured_client_id()
    return {"enabled": client_id is not None, "server_client_id": client_id}


def verify_google_token(token):
    client_id = configured_client_id()
    if client_id is None:
        raise HTTPException(503, "google_not_configured")
    try:
        # Bound Google's public signing-certificate fetch; never trust decoded-only JWTs.
        with requests.Session() as session:
            transport = GoogleRequest(session=session)

            def bounded_request(*args, **kwargs):
                kwargs["timeout"] = 5
                return transport(*args, **kwargs)

            claims = id_token.verify_oauth2_token(token, bounded_request, audience=client_id)
    except (TransportError, requests.RequestException):
        raise HTTPException(503, "google_temporarily_unavailable") from None
    except (ValueError, GoogleAuthError):
        raise HTTPException(401, "invalid_google_token") from None
    if (claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
            or claims.get("aud") != client_id
            or claims.get("email_verified") is not True
            or not isinstance(claims.get("sub"), str)
            or not 1 <= len(claims["sub"]) <= 255):
        raise HTTPException(401, "invalid_google_token")
    try:
        email = str(_email.validate_python(claims.get("email"))).lower()
    except ValidationError:
        raise HTTPException(401, "invalid_google_token") from None
    return claims["sub"], email, str(claims.get("name") or email.split("@")[0])[:80]


def _limit(request, db):
    consume_limit(db, "google-ip", request.client.host if request.client else "unknown", 60)


def _session_for(db, user):
    user.last_login = now()
    session = AuthSession(user_id=user.user_id, created_at=now(),
                          refresh_expires_at=now() + SESSION_LIFETIME,
                          password_change_required=False)
    return issue_pair(db, session, user)


@router.post("/login")
def login(data: GoogleToken, request: Request, db: Session = Depends(get_db)):
    _limit(request, db)
    subject, email, name = verify_google_token(data.id_token)
    identity = db.query(GoogleIdentity).filter_by(subject=subject).first()
    if identity is not None:
        return _session_for(db, identity.user)
    # Email alone never proves ownership of an existing WakeMate account.
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "google_existing_account_sign_in_first")
    user = User(name=name, email=email, hashed_password="!external:google")
    try:
        db.add(user)
        db.flush()
        user.google_identity = GoogleIdentity(subject=subject)
        db.add(UsersParams(user_id=user.user_id))
        db.flush()
        return _session_for(db, user)
    except IntegrityError:
        db.rollback()
        # Two sign-ins may create the same account concurrently. Never merge by email.
        identity = db.query(GoogleIdentity).filter_by(subject=subject).first()
        if identity is not None:
            return _session_for(db, identity.user)
        raise HTTPException(409, "google_existing_account_sign_in_first") from None


@router.post("/link")
def link(data: GoogleLink, request: Request, user: User = Depends(get_current_user),
         db: Session = Depends(get_db)):
    consume_limit(db, "google-link", str(user.user_id), 5)
    if request.state.auth_session.password_change_required:
        raise HTTPException(403, "password_change_required")
    if not has_password_login(user):
        raise HTTPException(400, "password_login_not_available")
    valid = False
    if len(data.current_password.encode("utf-8")) <= 72:
        with _kdf_slots:
            valid = passwords.verify(data.current_password, user.hashed_password)
    if not valid:
        raise HTTPException(400, "invalid_current_password")
    subject, email, _ = verify_google_token(data.id_token)
    if email != (user.email or "").lower():
        raise HTTPException(409, "google_email_mismatch")
    identity = db.query(GoogleIdentity).filter_by(subject=subject).first()
    if identity is not None and identity.user_id != user.user_id:
        raise HTTPException(409, "google_account_already_linked")
    if user.google_identity is not None:
        if user.google_identity.subject != subject:
            raise HTTPException(409, "google_account_already_linked")
        return {"status": "success", "google_linked": True}
    user.google_identity = GoogleIdentity(subject=subject)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "google_account_already_linked") from None
    return {"status": "success", "google_linked": True}
