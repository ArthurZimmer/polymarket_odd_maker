"""Smoke for B-11: Order.exit_policy controls stale-cancel behaviour.

Covers two paths in `TradingEngine._should_cancel_on_stale`:
  - SELL with exit_policy='ride'   → not cancelled on stale
  - SELL with exit_policy='cancel' → cancelled on stale
  - BUY always cancellable on stale
Also confirms PositionManager assigns:
  - STOP_LOSS    → 'cancel'
  - CONVERGENCE  → 'ride'
  - TIME_CRITICAL→ 'ride'
  - MANUAL       → 'ride'

Run: .venv/bin/python -m scripts.smoke_exit_policy
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.engine.position_manager import ExitReason, _POLICY_BY_REASON
from backend.engine.trading import (
    ORDER_STALE_TIMEOUT_S,
    TradingEngine,
)
from backend.models import EXIT_POLICY_CANCEL, EXIT_POLICY_RIDE, Order


def make_order(side: str, policy: str | None, submitted_at: datetime) -> Order:
    o = Order(
        polymarket_order_id="pm_smoke",
        token_id="SMOKE_EXIT_POLICY",
        outcome="Side",
        side=side,
        price=0.5,
        size=10.0,
        notional_usd=5.0,
        order_type="GTC",
        status="SUBMITTED",
        submitted_at=submitted_at,
        exit_policy=policy,
    )
    return o


def main() -> None:
    now = datetime.now(UTC)
    stale_ts = (now - timedelta(seconds=ORDER_STALE_TIMEOUT_S + 60)).replace(tzinfo=None)
    fresh_ts = (now - timedelta(seconds=10)).replace(tzinfo=None)

    # Stale checks
    sell_ride = make_order("SELL", EXIT_POLICY_RIDE, stale_ts)
    sell_cancel = make_order("SELL", EXIT_POLICY_CANCEL, stale_ts)
    buy = make_order("BUY", None, stale_ts)

    print(f"[1] SELL ride stale → is_stale={TradingEngine._is_stale(sell_ride, now)} "
          f"should_cancel={TradingEngine._should_cancel_on_stale(sell_ride)}")
    assert TradingEngine._is_stale(sell_ride, now) is True
    assert TradingEngine._should_cancel_on_stale(sell_ride) is False

    print(f"[2] SELL cancel stale → should_cancel={TradingEngine._should_cancel_on_stale(sell_cancel)}")
    assert TradingEngine._should_cancel_on_stale(sell_cancel) is True

    print(f"[3] BUY stale → should_cancel={TradingEngine._should_cancel_on_stale(buy)}")
    assert TradingEngine._should_cancel_on_stale(buy) is True

    # Fresh — none should be stale yet
    fresh = make_order("SELL", EXIT_POLICY_CANCEL, fresh_ts)
    print(f"[4] SELL cancel FRESH → is_stale={TradingEngine._is_stale(fresh, now)}")
    assert TradingEngine._is_stale(fresh, now) is False

    # Policy mapping
    print("\n[5] policy by reason:")
    for reason, expected in [
        (ExitReason.STOP_LOSS, EXIT_POLICY_CANCEL),
        (ExitReason.CONVERGENCE, EXIT_POLICY_RIDE),
        (ExitReason.TIME_CRITICAL, EXIT_POLICY_RIDE),
        (ExitReason.MANUAL, EXIT_POLICY_RIDE),
    ]:
        actual = _POLICY_BY_REASON[reason]
        print(f"    {reason:<22} → {actual}")
        assert actual == expected

    print("\nDone — exit_policy honoured by the poller.")


if __name__ == "__main__":
    main()
