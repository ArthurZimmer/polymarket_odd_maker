"""Wallet configuration endpoints — inject credentials, view address + USDC balance."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth import require_auth
from backend.crypto.vault import VaultLocked, VaultState
from backend.db import get_session
from backend.models import WalletConfig
from backend.polymarket.clob_client import invalidate_balance_cache
from backend.utils.blockchain import derive_address, fetch_usdc_balance

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


class WalletPayload(BaseModel):
    # Polygon private key: 64 hex chars (32 bytes) optionally prefixed with 0x.
    private_key: str = Field(min_length=64, max_length=66)
    api_key: str | None = None
    api_secret: str | None = None
    funder_address: str | None = None


class WalletView(BaseModel):
    address: str | None
    has_credentials: bool
    has_api_key: bool
    funder_address: str | None
    usdc_balance: float | None


async def _load(session: AsyncSession) -> WalletConfig | None:
    res = await session.execute(select(WalletConfig).where(WalletConfig.id == 1))
    return res.scalar_one_or_none()


async def _to_view(row: WalletConfig | None, *, fetch_balance: bool = True) -> WalletView:
    if row is None:
        return WalletView(
            address=None,
            has_credentials=False,
            has_api_key=False,
            funder_address=None,
            usdc_balance=None,
        )
    balance: float | None = None
    if fetch_balance:
        # Tradable balance lives on the Polymarket proxy wallet (`funder`)
        # when one is configured. EOA only holds the balance for users who
        # trade without a proxy — fall back to that.
        target = row.funder_address or row.address
        try:
            balance = await fetch_usdc_balance(target)
        except Exception:
            logger.exception("Failed to fetch pUSD balance for %s", target)
    return WalletView(
        address=row.address,
        has_credentials=True,
        has_api_key=row.encrypted_api_key is not None,
        funder_address=row.funder_address,
        usdc_balance=balance,
    )


@router.get("", response_model=WalletView)
async def get_wallet(
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> WalletView:
    row = await _load(session)
    return await _to_view(row)


@router.put("", response_model=WalletView)
async def put_wallet(
    body: WalletPayload,
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> WalletView:
    try:
        address = derive_address(body.private_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid private key: {exc}") from exc
    try:
        enc_pk = VaultState.encrypt(body.private_key)
        enc_ak = VaultState.encrypt(body.api_key) if body.api_key else None
        enc_as = VaultState.encrypt(body.api_secret) if body.api_secret else None
    except VaultLocked as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vault locked — log in again.",
        ) from exc
    row = await _load(session)
    if row is None:
        row = WalletConfig(id=1, address=address, encrypted_private_key=enc_pk)
        session.add(row)
    else:
        row.address = address
        row.encrypted_private_key = enc_pk
    row.encrypted_api_key = enc_ak
    row.encrypted_api_secret = enc_as
    row.funder_address = body.funder_address
    await session.commit()
    await session.refresh(row)
    # Wallet changed → the cached USDC balance now belongs to the previous
    # wallet. Next risk-gate evaluation must hit the chain fresh.
    invalidate_balance_cache()
    return await _to_view(row)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wallet(
    _user: str = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await _load(session)
    if row is not None:
        await session.delete(row)
        await session.commit()
    invalidate_balance_cache()
