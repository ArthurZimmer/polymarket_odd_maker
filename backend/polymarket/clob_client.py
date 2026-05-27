"""Thin wrapper around py-clob-client.

Why a wrapper: py-clob-client is synchronous-only and reads credentials from
constructor args. We need an async-friendly interface that pulls the latest
decrypted wallet from VaultState at *every* operation so the bot can pick up
wallet edits or rotations without restart.

Auth model
----------
Polymarket CLOB requires:
  1. A funded EOA / proxy wallet (the `signer`).
  2. A `creds` block (api_key, api_secret, api_passphrase) which the user
     creates inside Polymarket once per wallet.

If the user only has private_key configured we can still place orders signed
by the EOA, but reads (open orders, etc.) need the API key. For now we
require both.

Locking
-------
VaultState.decrypt blocks (CPU-only) — wrap calls in `asyncio.to_thread` to
keep the event loop responsive when the rest of the trading engine is async.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.crypto.vault import VaultLocked, VaultState
from backend.models import WalletConfig

logger = logging.getLogger(__name__)

CHAIN_ID = POLYGON  # 137


class WalletNotConfigured(Exception):
    pass


@dataclass(slots=True)
class WalletCredentials:
    address: str
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str  # Polymarket calls this `passphrase`; stored same column as api_secret? no — separate
    funder: str | None = None


async def load_wallet_credentials(session: AsyncSession) -> WalletCredentials:
    """Pull the wallet row, decrypt via VaultState, return credentials.

    Raises VaultLocked when vault is locked; WalletNotConfigured when there
    is no wallet row yet.
    """
    row = (
        await session.execute(select(WalletConfig).where(WalletConfig.id == 1))
    ).scalar_one_or_none()
    if row is None:
        raise WalletNotConfigured("no wallet configured")
    if not VaultState.is_unlocked():
        raise VaultLocked()
    private_key = VaultState.decrypt(row.encrypted_private_key)
    # Etapa 2's wallet schema only stores api_key + api_secret. Polymarket's
    # creds also need a passphrase — for V1 we read it from the api_secret
    # column as `secret:passphrase` (set during setup). Fall back to empty.
    api_key = VaultState.decrypt(row.encrypted_api_key) if row.encrypted_api_key else ""
    api_secret_raw = (
        VaultState.decrypt(row.encrypted_api_secret) if row.encrypted_api_secret else ""
    )
    if ":" in api_secret_raw:
        api_secret, api_passphrase = api_secret_raw.split(":", 1)
    else:
        api_secret = api_secret_raw
        api_passphrase = ""
    return WalletCredentials(
        address=row.address,
        private_key=private_key,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        funder=row.funder_address,
    )


def _build_client(creds: WalletCredentials) -> ClobClient:
    """Spin up a fresh CLOB client. Cheap enough to do per-call — keeps us
    out of the cache-invalidation game when credentials change.
    """
    api = ApiCreds(
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        api_passphrase=creds.api_passphrase,
    )
    client = ClobClient(
        host=settings.polymarket_clob_url,
        key=creds.private_key,
        chain_id=CHAIN_ID,
        creds=api,
        # If funder is set we're using a Polymarket proxy wallet
        # (signature_type=2 for safe / 1 for proxy); leave default to let
        # the client auto-detect from the on-chain proxy mapping.
        funder=creds.funder,
    )
    return client


@dataclass(slots=True)
class PlacedOrder:
    """Subset of the CLOB response we actually use downstream."""

    polymarket_order_id: str | None
    status: str   # 'live' | 'matched' | 'delayed' | 'unmatched' | ...
    success: bool
    error_msg: str | None = None
    raw: dict[str, Any] | None = None


async def place_limit_order(
    session: AsyncSession,
    *,
    token_id: str,
    price: float,
    size: float,
    side: str,
    order_type: str = "GTC",
) -> PlacedOrder:
    """Build, sign, post a single LIMIT order. Returns parsed result.

    `size` is in shares (each share resolves to $1 on a winning outcome).
    Polymarket prices are clamped at [0.001, 0.999]; the caller is
    responsible for sanity checking the price.
    """
    import asyncio

    creds = await load_wallet_credentials(session)

    def _do() -> dict[str, Any]:
        client = _build_client(creds)
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,  # "BUY" or "SELL"
        )
        # `create_and_post_order` signs locally and submits the signed
        # order to the CLOB. Returns the API response dict.
        signed = client.create_order(args)
        ot = getattr(OrderType, order_type, OrderType.GTC)
        return client.post_order(signed, ot)

    try:
        resp = await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Polymarket order failed: token=%s side=%s price=%s size=%s",
            token_id,
            side,
            price,
            size,
        )
        return PlacedOrder(
            polymarket_order_id=None,
            status="ERROR",
            success=False,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
    # Response shape (per docs):
    #   { "success": True, "errorMsg": "", "orderID": "...", "transactionsHashes": [...], "status": "live" }
    return PlacedOrder(
        polymarket_order_id=resp.get("orderID") or resp.get("order_id"),
        status=str(resp.get("status") or "unknown"),
        success=bool(resp.get("success", True)),
        error_msg=resp.get("errorMsg") or None,
        raw=resp,
    )


async def get_order(session: AsyncSession, order_id: str) -> dict[str, Any] | None:
    """Fetch a single order by Polymarket order id. None if not found."""
    import asyncio

    creds = await load_wallet_credentials(session)

    def _do() -> Any:
        client = _build_client(creds)
        return client.get_order(order_id)

    try:
        return await asyncio.to_thread(_do)
    except Exception:
        logger.exception("Polymarket get_order failed: %s", order_id)
        return None


# In-memory cache for USDC balance reads — Polymarket RPC is slow enough
# that hitting it every TradingEngine cycle (5s) is wasteful. Refresh every
# BALANCE_CACHE_TTL_S seconds at most.
_BALANCE_CACHE: dict[str, Any] = {"value": None, "fetched_at": 0.0}
BALANCE_CACHE_TTL_S = 30.0


async def get_usdc_balance(
    session: AsyncSession, *, force_refresh: bool = False
) -> float | None:
    """Return on-chain USDC collateral balance for the configured wallet.

    Returns None if vault is locked, wallet not configured, or the call
    fails. Caller treats None as "skip stake check, but other risk gates
    still apply." Result is cached in memory for BALANCE_CACHE_TTL_S to
    avoid spamming the Polymarket API every trading cycle.
    """
    import asyncio
    import time

    now = time.time()
    if (
        not force_refresh
        and _BALANCE_CACHE["value"] is not None
        and now - _BALANCE_CACHE["fetched_at"] < BALANCE_CACHE_TTL_S
    ):
        return _BALANCE_CACHE["value"]

    if not VaultState.is_unlocked():
        return None
    try:
        creds = await load_wallet_credentials(session)
    except (VaultLocked, WalletNotConfigured):
        return None

    def _do() -> dict[str, Any] | None:
        client = _build_client(creds)
        return client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )

    try:
        resp = await asyncio.to_thread(_do)
    except Exception:
        logger.exception("Polymarket get_balance_allowance failed")
        return None
    if not resp:
        return None
    # Polymarket returns balance as a string in USDC base units (1e6 = $1).
    raw = resp.get("balance") if isinstance(resp, dict) else None
    if raw is None:
        return None
    try:
        balance_usd = float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return None
    _BALANCE_CACHE["value"] = balance_usd
    _BALANCE_CACHE["fetched_at"] = now
    return balance_usd


async def cancel_order(session: AsyncSession, order_id: str) -> bool:
    import asyncio

    creds = await load_wallet_credentials(session)

    def _do() -> dict[str, Any]:
        client = _build_client(creds)
        return client.cancel(order_id=order_id)

    try:
        resp = await asyncio.to_thread(_do)
        return bool(resp.get("not_canceled") is None or not resp.get("not_canceled"))
    except Exception:
        logger.exception("Polymarket cancel failed: %s", order_id)
        return False
