"""Smoke test for Etapa 12 history endpoints.

Inserts a mix of synthetic CLOSED positions spread across the last 10
days, plus 1 OPEN with an OddsSnapshot for live-pnl, then hits all
history endpoints + CSV export and prints the highlights.

Cleans up before and after.

Run: .venv/bin/python -m scripts.smoke_history
"""
from __future__ import annotations

import asyncio
import csv
import io
import urllib.request
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from backend.db import SessionLocal
from backend.models import OddsSnapshot, Position

FAKE_TOKEN_PREFIX = "SMOKE_HIST_E12_"
API_BASE = "http://127.0.0.1:8000"
TEST_PASSWORD = "test-password-456"


async def cleanup(session) -> None:
    await session.execute(
        delete(Position).where(Position.token_id.like(f"{FAKE_TOKEN_PREFIX}%"))
    )
    await session.execute(
        delete(OddsSnapshot).where(OddsSnapshot.token_id.like(f"{FAKE_TOKEN_PREFIX}%"))
    )
    await session.commit()


async def seed(session) -> None:
    # 8 CLOSED positions across 10 days, mix of wins and losses.
    now = datetime.now(UTC)
    plan = [
        (9, 8.0),   # 9 days ago, +$8
        (7, -3.0),  # 7 days ago, -$3
        (5, 12.5),  # 5 days ago, +$12.5
        (5, -2.0),  # same day, -$2
        (3, 4.0),   # 3 days ago, +$4
        (2, -6.0),  # 2 days ago, -$6
        (1, 9.0),   # 1 day ago, +$9
        (0, 5.0),   # today, +$5
    ]
    for i, (days_ago, pnl) in enumerate(plan):
        exit_at = (now - timedelta(days=days_ago, hours=2)).replace(tzinfo=None)
        entry_at = exit_at - timedelta(hours=1)
        size = 20.0
        entry_price = 0.50
        exit_price = entry_price + (pnl / size)
        session.add(
            Position(
                polymarket_event_id=f"smoke_hist_event_{i}",
                token_id=f"{FAKE_TOKEN_PREFIX}{i}",
                outcome="Side",
                size=size,
                entry_price=entry_price,
                entry_at=entry_at,
                exit_price=exit_price,
                exit_at=exit_at,
                pnl_usd=pnl,
                status="CLOSED",
            )
        )
    # 1 OPEN position + a polymarket snapshot for live-pnl.
    open_token = f"{FAKE_TOKEN_PREFIX}OPEN"
    session.add(
        Position(
            polymarket_event_id="smoke_hist_event_open",
            token_id=open_token,
            outcome="Side",
            size=10.0,
            entry_price=0.55,
            entry_at=now.replace(tzinfo=None) - timedelta(minutes=20),
            status="OPEN",
        )
    )
    session.add(
        OddsSnapshot(
            captured_at=now.replace(tzinfo=None),
            source="polymarket",
            event_id="polymarket:smoke",
            outcome="Side",
            token_id=open_token,
            best_bid=0.60,
            best_ask=0.62,
            ask_depth_usd=150.0,
        )
    )
    await session.commit()


def http(path: str, token: str) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(API_BASE + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def login() -> str:
    req = urllib.request.Request(
        API_BASE + "/api/auth/login",
        data=b'{"password":"' + TEST_PASSWORD.encode() + b'"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        import json
        return json.load(resp)["access_token"]


async def main() -> None:
    async with SessionLocal() as session:
        await cleanup(session)
        await seed(session)
        print("Seeded 8 CLOSED + 1 OPEN positions")

    # Now hit the API via the running server (uvicorn reload caught the new code).
    token = login()
    import json

    code, body, _ = http("/api/history/summary", token)
    summary = json.loads(body)
    print(f"\n[summary] closed={summary['closed_positions']} "
          f"total_pnl=${summary['total_pnl_usd']:.2f} "
          f"win_rate={summary['win_rate_pct']}% "
          f"today=${summary['realized_pnl_today_usd']:.2f}")
    assert summary["closed_positions"] >= 8
    assert summary["win_rate_pct"] is not None

    code, body, _ = http("/api/history/pnl-daily?days=15", token)
    points = json.loads(body)
    print(f"[pnl-daily] {len(points)} dias com PnL: "
          + ", ".join(f"{p['date']}:${p['pnl_usd']}" for p in points))
    assert len(points) > 0
    assert all("cumulative_pnl_usd" in p for p in points)
    # Final cumulative should equal sum of pnl_usd of the seeded last-10d trades.
    total = sum(p["pnl_usd"] for p in points)
    print(f"          cumulativo final: ${points[-1]['cumulative_pnl_usd']:.2f} (soma={total:.2f})")

    code, body, _ = http("/api/positions/live-pnl", token)
    live = json.loads(body)
    print(f"\n[live-pnl] {len(live)} posições, primeira:")
    if live:
        print(f"          {live[0]}")
    assert any(l["unrealized_pnl_usd"] is not None for l in live)

    code, body, headers = http("/api/history/export.csv", token)
    cd = headers.get("Content-Disposition", "")
    print(f"\n[export.csv] {len(body)} bytes, Content-Disposition: {cd}")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    print(f"             header: {rows[0]}")
    print(f"             {len(rows) - 1} data rows")
    assert len(rows) >= 9  # header + 8 closed + 1 open

    # Cleanup
    async with SessionLocal() as session:
        await cleanup(session)
    print("\nCleaned up. Done.")


if __name__ == "__main__":
    asyncio.run(main())
