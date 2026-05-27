"""Smoke test for Etapa 13 ProxyPool + auto-failover.

Exercises:
  1. Parsing of WEBSHARE_PROXIES CSV (3 proxies).
  2. Round-robin: 3 sequential next_proxy() calls return all 3 distinct.
  3. mark_failure 3x burns a proxy → drops out of rotation until cooldown.
  4. proxied_get_sync mock: fake session returns 429 → rotates + counts blocks.
  5. proxied_get_sync mock: fake session raises → rotates + counts net errs.
  6. Recovery path: a successful response after one failure clears the
     consecutive-failures counter on the surviving proxy.

Run: .venv/bin/python -m scripts.smoke_proxies
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.scrapers.base import ScraperStats
from backend.scrapers.proxies import (
    ProxyPool,
    is_block_status,
    proxied_get_sync,
    reset_for_tests,
)
import backend.scrapers.proxies as proxies_mod


class FakeResp:
    def __init__(self, status_code: int, body: str = "ok"):
        self.status_code = status_code
        self.text = body

    def json(self) -> Any:
        return []


class FakeSession:
    def __init__(self, plan: list[Any]):
        # plan = list of (status_or_exception, proxy_seen_label) tuples that
        # this session emits in order, regardless of what URL is requested.
        self.plan = list(plan)
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, "kwargs": dict(kwargs)})
        if not self.plan:
            return FakeResp(200)
        item = self.plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResp(item)


def main() -> None:
    # 1. Parse 3 proxies.
    raw = "1.1.1.1:8000:u:p,2.2.2.2:8000:u:p,3.3.3.3:8000:u:p"
    pool = reset_for_tests(raw)
    assert pool.has_proxies
    assert pool.stats()["total"] == 3
    print(f"[1] Parsed 3 proxies: {[p.label for p in pool._proxies]}")

    # 2. Round-robin distinct order.
    seen = [pool.next_proxy().label for _ in range(3)]
    print(f"[2] Round-robin 3 next: {seen}")
    assert len(set(seen)) == 3

    # 3. Burn one proxy with 3 consecutive failures.
    target = pool._proxies[0]
    for _ in range(3):
        pool.mark_failure(target)
    assert target.is_in_cooldown(), "expected burned proxy in cooldown"
    print(f"[3] Burned {target.label} → cooldown_until={target.cooldown_until}")
    # Next 4 picks should never return the burned one.
    picks = [pool.next_proxy().label for _ in range(4)]
    print(f"[3] Next 4 picks (skip burned): {picks}")
    assert target.label not in picks

    # 4. proxied_get_sync with all blocked: rotates + counts blocks.
    pool = reset_for_tests(raw)  # reset
    stats = ScraperStats(name="smoke")
    sess = FakeSession([429, 403, 503])  # 3 attempts, all blocked
    resp = proxied_get_sync(sess, "https://example.com/x", stats=stats, max_retries=3)
    assert resp.status_code in {429, 403, 503}
    assert stats.block_count == 3
    assert stats.network_errors == 0
    # Each attempt should have used a *different* proxy (round-robin).
    seen_proxies = [call["kwargs"]["proxies"]["http"] for call in sess.calls]
    print(f"[4] 3x blocked, proxies tried: {seen_proxies}")
    print(f"[4] stats: block_count={stats.block_count} last_blocked_at={stats.last_blocked_at}")
    assert len(set(seen_proxies)) == 3

    # 5. proxied_get_sync with exceptions: rotates + counts network_errors.
    pool = reset_for_tests(raw)
    stats2 = ScraperStats(name="smoke2")
    sess2 = FakeSession([RuntimeError("conn refused"), TimeoutError("timeout"), 200])
    resp2 = proxied_get_sync(sess2, "https://example.com/x", stats=stats2, max_retries=3)
    assert resp2.status_code == 200
    assert stats2.network_errors == 2
    assert stats2.block_count == 0
    print(f"[5] 2 net errors + 1 success: net_errors={stats2.network_errors} ok=200")

    # 6. Recovery: after the 2 failures, the surviving proxy that returned
    #    200 should have its consecutive_failures cleared.
    survivor = next(p for p in proxies_mod.proxy_pool._proxies if p.success_count > 0)
    assert survivor.consecutive_failures == 0
    print(f"[6] Survivor {survivor.label}: success_count={survivor.success_count} "
          f"failures_consec={survivor.consecutive_failures}")

    # 7. is_block_status helper.
    assert is_block_status(429) and is_block_status(403) and is_block_status(503)
    assert not is_block_status(200) and not is_block_status(None)
    print("[7] is_block_status passes 429/403/503; rejects 200/None")

    # Cleanup: empty pool so the dev server returns to direct mode.
    reset_for_tests(None)
    print("\nDone — pool reset.")


if __name__ == "__main__":
    main()
