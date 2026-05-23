"""Polygon helpers: derive address from private key, fetch USDC.e balance."""
from __future__ import annotations

import logging

from eth_account import Account
from web3 import AsyncWeb3
from web3.providers.rpc import AsyncHTTPProvider

from backend.config import settings

logger = logging.getLogger(__name__)

# USDC.e (bridged) on Polygon — the token Polymarket settles in
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


async def fetch_usdc_balance(address: str) -> float:
    w3 = AsyncWeb3(AsyncHTTPProvider(settings.polygon_rpc_url))
    contract = w3.eth.contract(address=USDC_E_ADDRESS, abi=ERC20_BALANCEOF_ABI)
    checksummed = AsyncWeb3.to_checksum_address(address)
    raw = await contract.functions.balanceOf(checksummed).call()
    return raw / (10**USDC_E_DECIMALS)
