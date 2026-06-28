"""Authentication endpoints: login, logout, and current-user lookup.

Login validates argon2id credentials, stamps ``last_login_at``, and stores a
minimal user payload in the signed session cookie. These endpoints are NOT
guarded by ``require_user`` (login must be reachable while logged out).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from folio_core.config import get_settings
from folio_core.logging import get_logger

from ..auth import authenticate, authenticate_token
from ..deps import SESSION_USER_KEY, get_current_user, get_db
from ..schemas import LoginRequest, MeResponse, OkResponse

logger = get_logger("portal.routers.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=MeResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MeResponse:
    # Token path: verify against the argon2id hash; establish the same session.
    if payload.token is not None:
        if not authenticate_token(payload.token):
            # Generic message: never reveal which field/mode was wrong.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        settings = get_settings()
        display = settings.admin_username or "token"
        request.session[SESSION_USER_KEY] = {"id": None, "username": display}
        logger.info("auth.login username=%s method=token", display)
        return MeResponse(username=display)

    # Password path: validate against the users table and stamp last_login.
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        # Generic message: do not leak whether the username exists.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    db.commit()
    request.session[SESSION_USER_KEY] = {"id": user.id, "username": user.username}
    logger.info("auth.login username=%s method=password", user.username)
    return MeResponse(username=user.username)


@router.post("/logout", response_model=OkResponse)
def logout(request: Request) -> OkResponse:
    request.session.pop(SESSION_USER_KEY, None)
    return OkResponse()


@router.get("/me", response_model=MeResponse)
def me(request: Request) -> MeResponse:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return MeResponse(username=user["username"])
