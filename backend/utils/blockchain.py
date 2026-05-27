"""Polygon helpers: derive address from private key, fetch pUSD balance.

Polymarket migrated their collateral to **pUSD** (Polymarket USD,
ERC-20 0xc011a7e1...82dfb, 6 decimals) — the old USDC.e is no longer
where users hold their tradable balance. Display + risk-gate code reads
pUSD directly via the standard ERC-20 `balanceOf`.
"""
from __future__ import annotations

import logging

from eth_account import Account
from web3 import AsyncWeb3
from web3.providers.rpc import AsyncHTTPProvider

from backend.config import settings

logger = logging.getLogger(__name__)

# Polymarket USD (pUSD) — the current collateral token holders use to
# trade on the CLOB. Replaces the older USDC.e (0x2791Bca1...A84174).
PUSD_ADDRESS = AsyncWeb3.to_checksum_address("0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb")
PUSD_DECIMALS = 6

# Kept as a constant for code that still wants to consult the legacy bridge
# token (e.g. health dashboards) — not used in the trading path anymore.
USDC_E_ADDRESS = AsyncWeb3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
USDC_E_DECIMALS = 6

ERC20_BALANCEOF_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]


def derive_address(private_key: str) -> str:
    pk = private_key if private_key.startswith("0x") else "0x" + private_key
    return Account.from_key(pk).address


async def fetch_pusd_balance(address: str) -> float:
    """Read pUSD balance for `address` from the Polygon RPC.

    Polymarket users with a proxy wallet hold pUSD on the proxy address
    (the `funder`), not on the EOA — callers wanting the tradable balance
    should pass the funder when one is configured.
    """
    w3 = AsyncWeb3(AsyncHTTPProvider(settings.polygon_rpc_url))
    contract = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_BALANCEOF_ABI)
    checksummed = AsyncWeb3.to_checksum_address(address)
    raw = await contract.functions.balanceOf(checksummed).call()
    return raw / (10**PUSD_DECIMALS)


# Backwards-compat alias — wallet.py + clob_client.py still call this name.
# Returns the *tradable* balance, which today is pUSD.
fetch_usdc_balance = fetch_pusd_balance
