"""Smoke for B-7: _BALANCE_CACHE invalidates on wallet change.

We poke the cache directly (rather than hit real RPC) — sets a value via
the module global, then calls invalidate_balance_cache() and confirms the
cache reverted to "never fetched" so the next get_usdc_balance() call will
actually hit the chain.

Run: .venv/bin/python -m scripts.smoke_balance_cache
"""
from __future__ import annotations

import time

import backend.polymarket.clob_client as clob


def main() -> None:
    # 1. Seed the cache like a real fetch would
    clob._BALANCE_CACHE["value"] = 42.50
    clob._BALANCE_CACHE["fetched_at"] = time.time()
    print(f"[1] seeded cache: value={clob._BALANCE_CACHE['value']}")
    assert clob._BALANCE_CACHE["value"] == 42.50

    # 2. Invalidate
    clob.invalidate_balance_cache()
    print(
        f"[2] after invalidate: value={clob._BALANCE_CACHE['value']} "
        f"fetched_at={clob._BALANCE_CACHE['fetched_at']}"
    )
    assert clob._BALANCE_CACHE["value"] is None
    assert clob._BALANCE_CACHE["fetched_at"] == 0.0

    print("\nDone — wallet PUT/DELETE will now force a fresh read.")


if __name__ == "__main__":
    main()
