"""Authentication: master password unlocks the vault, JWT bearer protects endpoints.

JWT secret is regenerated per process — restart invalidates all tokens. Acceptable for
single-user local-only deployment.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from backend.config import settings
from backend.crypto.vault import VaultState

router = APIRouter(prefix="/api/auth", tags=["auth"])

_JWT_SECRET = secrets.token_urlsafe(64)
_security = HTTPBearer(auto_error=False)


class PasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class AuthStateView(BaseModel):
    setup_required: bool
    unlocked: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _issue_token() -> TokenResponse:
    ttl = timedelta(minutes=settings.jwt_ttl_minutes)
    expire = datetime.now(UTC) + ttl
    payload = {"sub": "user", "exp": expire}
    token = jwt.encode(payload, _JWT_SECRET, algorithm=settings.jwt_algorithm)
    return TokenResponse(access_token=token, expires_in=int(ttl.total_seconds()))


@router.get("/state", response_model=AuthStateView)
async def auth_state() -> AuthStateView:
    return AuthStateView(
        setup_required=not VaultState.is_setup(),
        unlocked=VaultState.is_unlocked(),
    )


@router.post("/setup", response_model=TokenResponse)
async def setup(body: PasswordBody) -> TokenResponse:
    if VaultState.is_setup():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Vault already initialized — use /api/auth/login instead.",
        )
    VaultState.setup(body.password)
    return _issue_token()


@router.post("/login", response_model=TokenResponse)
async def login(body: PasswordBody) -> TokenResponse:
    if not VaultState.is_setup():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vault not initialized — use /api/auth/setup first.",
        )
    if not VaultState.unlock(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password."
        )
    return _issue_token()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> None:
    VaultState.lock()


def require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth")
    try:
        payload = jwt.decode(
            creds.credentials, _JWT_SECRET, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    if not VaultState.is_unlocked():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vault is locked (process restarted?) — log in again.",
        )
    return payload.get("sub", "user")
