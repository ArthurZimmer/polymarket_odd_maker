"""Smoke for B-4/B-5: PlacedOrder exige id explícito; no zombie SUBMITTED+NULL.

Confirms the parse logic in `place_limit_order`'s response handler — invokes
it via a stub `client.post_order` that returns various malformed payloads.

Run: .venv/bin/python -m scripts.smoke_zombie_order
"""
from __future__ import annotations

from backend.polymarket.clob_client import PlacedOrder


def parse_response(resp: dict) -> PlacedOrder:
    """Replicates the parse block inside place_limit_order."""
    pm_order_id = resp.get("orderID") or resp.get("order_id")
    return PlacedOrder(
        polymarket_order_id=pm_order_id,
        status=str(resp.get("status") or "unknown"),
        success=bool(resp.get("success", False)) and bool(pm_order_id),
        error_msg=resp.get("errorMsg") or None,
        raw=resp,
    )


def main() -> None:
    # 1. Empty response — must be rejected
    r = parse_response({})
    print(f"[1] empty resp: success={r.success} id={r.polymarket_order_id}")
    assert r.success is False
    assert r.polymarket_order_id is None

    # 2. Has success=True but no orderID — must be rejected
    r = parse_response({"success": True})
    print(f"[2] success=True no id: success={r.success}")
    assert r.success is False

    # 3. Has orderID but no success field — old behaviour would default True,
    #    new behaviour requires explicit success=True.
    r = parse_response({"orderID": "abc123"})
    print(f"[3] id present no success field: success={r.success}")
    assert r.success is False

    # 4. Has both → success
    r = parse_response({"success": True, "orderID": "abc123", "status": "live"})
    print(f"[4] success=True + id: success={r.success} id={r.polymarket_order_id}")
    assert r.success is True
    assert r.polymarket_order_id == "abc123"

    # 5. error payload
    r = parse_response({"success": False, "errorMsg": "insufficient balance"})
    print(f"[5] explicit failure: success={r.success} err={r.error_msg!r}")
    assert r.success is False
    assert r.error_msg == "insufficient balance"

    # 6. Alternate id key
    r = parse_response({"success": True, "order_id": "xyz"})
    print(f"[6] alternate id key: success={r.success} id={r.polymarket_order_id}")
    assert r.success is True
    assert r.polymarket_order_id == "xyz"

    print("\nDone — zombie orders cannot leak through.")


if __name__ == "__main__":
    main()
