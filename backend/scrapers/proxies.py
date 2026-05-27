"""Datacenter proxy pool for outbound scraping.

Loads `WEBSHARE_PROXIES` (or `settings.webshare_proxies`) once at startup
and exposes round-robin rotation with auto-failover:
  - `next_proxy()` returns the next non-cooldown proxy.
  - `mark_failure(proxy)` increments consecutive failures; at the
    `PROXY_BLOCK_THRESHOLD`, the proxy is parked for `PROXY_COOLDOWN_MINUTES`.
  - `mark_success(proxy)` clears the failure counter.

A proxy is parsed from `host:port:user:pass`. Empty pool ⇒ scrapers fall
back to direct connection (None is a valid `next_proxy()` result).

Blocked-status classification lives at `is_block_status(http_code)` —
imported by per-bookmaker scrapers to keep that policy centralized.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

# HTTP status codes treated as "block / rate limit" — the proxy gets a
# failure mark. 5xx is conservative on purpose: it might be the upstream
# service hiccupping rather than the proxy, but we'd still rather rotate
# than hammer the same exit IP through the failure.
BLOCKED_STATUSES = frozenset({429, 403, 451, 502, 503, 504})


def is_block_status(code: int | None) -> bool:
    if code is None:
        return False
    return code in BLOCKED_STATUSES


@dataclass
class Proxy:
    host: str
    port: int
    user: str | None
    password: str | None
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    last_used_at: datetime | None = None
    success_count: int = 0
    failure_count: int = 0

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    def url_for(self, scheme: str = "http") -> str:
        auth = ""
        if self.user is not None and self.password is not None:
            auth = f"{self.user}:{self.password}@"
        return f"{scheme}://{auth}{self.host}:{self.port}"

    def to_proxies_dict(self) -> dict[str, str]:
        # curl_cffi / requests-compatible: same URL for http and https
        # (we tunnel via CONNECT for HTTPS).
        url = self.url_for("http")
        return {"http": url, "https": url}

    def is_in_cooldown(self, now: datetime | None = None) -> bool:
        if self.cooldown_until is None:
            return False
        now = now or datetime.now(UTC)
        return now < self.cooldown_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "consecutive_failures": self.consecutive_failures,
            "in_cooldown": self.is_in_cooldown(),
            "cooldown_until": (
                self.cooldown_until.isoformat() if self.cooldown_until else None
            ),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }


def _parse_proxies(raw: str | None) -> list[Proxy]:
    if not raw:
        return []
    out: list[Proxy] = []
    for chunk in raw.split(","):
        s = chunk.strip()
        if not s:
            continue
        parts = s.split(":")
        if len(parts) == 2:
            host, port = parts
            user = password = None
        elif len(parts) == 4:
            host, port, user, password = parts
        else:
            logger.warning("Ignoring malformed proxy entry: %r", s)
            continue
        try:
            out.append(
                Proxy(host=host, port=int(port), user=user, password=password)
            )
        except ValueError:
            logger.warning("Ignoring proxy with non-int port: %r", s)
    return out


class ProxyPool:
    """Thread-safe round-robin proxy rotation with cooldown."""

    def __init__(self, proxies: list[Proxy] | None = None) -> None:
        self._proxies: list[Proxy] = proxies or []
        self._cursor: int = 0
        self._lock = threading.Lock()
        self._block_threshold = settings.proxy_block_threshold
        self._cooldown = timedelta(minutes=settings.proxy_cooldown_minutes)
        if self._proxies:
            logger.info("ProxyPool initialised with %d proxies", len(self._proxies))
        else:
            logger.info("ProxyPool empty — scrapers will run direct")

    @property
    def has_proxies(self) -> bool:
        return bool(self._proxies)

    def next_proxy(self) -> Proxy | None:
        """Return the next non-cooldown proxy, or None if pool is empty
        or every proxy is currently parked.
        """
        if not self._proxies:
            return None
        now = datetime.now(UTC)
        with self._lock:
            n = len(self._proxies)
            for _ in range(n):
                p = self._proxies[self._cursor]
                self._cursor = (self._cursor + 1) % n
                if not p.is_in_cooldown(now):
                    p.last_used_at = now
                    return p
        return None  # every proxy in cooldown

    def mark_failure(self, proxy: Proxy) -> None:
        with self._lock:
            proxy.consecutive_failures += 1
            proxy.failure_count += 1
            if proxy.consecutive_failures >= self._block_threshold:
                proxy.cooldown_until = datetime.now(UTC) + self._cooldown
                logger.warning(
                    "Proxy %s parked until %s (%d consec failures)",
                    proxy.label,
                    proxy.cooldown_until.isoformat(),
                    proxy.consecutive_failures,
                )
                proxy.consecutive_failures = 0

    def mark_success(self, proxy: Proxy) -> None:
        with self._lock:
            proxy.consecutive_failures = 0
            proxy.success_count += 1
            # If we're succeeding again after a cooldown was scheduled but
            # not yet elapsed, leave the cooldown alone — the policy is
            # "rest no matter what once burned" until the timer expires.

    def stats(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        active = sum(1 for p in self._proxies if not p.is_in_cooldown(now))
        cooling = len(self._proxies) - active
        return {
            "total": len(self._proxies),
            "active": active,
            "in_cooldown": cooling,
            "block_threshold": self._block_threshold,
            "cooldown_minutes": int(self._cooldown.total_seconds() / 60),
            "proxies": [p.to_dict() for p in self._proxies],
        }


# Module-level singleton — scrapers import this; tests can replace it.
proxy_pool = ProxyPool(_parse_proxies(settings.webshare_proxies))


def reset_for_tests(raw: str | None) -> ProxyPool:
    """Rebuild the singleton with a fresh proxy list. Test-only helper."""
    global proxy_pool
    proxy_pool = ProxyPool(_parse_proxies(raw))
    return proxy_pool


def proxied_get_sync(
    session: Any,
    url: str,
    *,
    max_retries: int = 3,
    timeout: int = 30,
    stats: Any = None,
    **kwargs: Any,
) -> Any:
    """Synchronous GET with proxy rotation and auto-failover.

    Designed for `curl_cffi.requests.Session` but only needs `.get(...)` to
    accept a `proxies` kwarg and return a response with `.status_code`.

    Behaviour per attempt:
      1. Pick next non-cooldown proxy from the pool (or None = direct).
      2. Issue GET. On network exception OR a blocked status, mark the
         proxy as failed and try the next one.
      3. On success status, mark the proxy as successful and return.
    After `max_retries` attempts, returns the last response received (which
    may carry a blocked status — caller decides how to surface that), or
    re-raises the last exception if every attempt threw.

    `stats` is an optional ScraperStats; when provided we record the last
    proxy used, the latest block timestamp, and counters.
    """
    last_resp: Any = None
    last_exc: BaseException | None = None
    for _ in range(max(1, max_retries)):
        proxy = proxy_pool.next_proxy()
        kw = dict(kwargs)
        kw["timeout"] = timeout
        if proxy is not None:
            kw["proxies"] = proxy.to_proxies_dict()
        if stats is not None:
            stats.last_proxy = proxy.label if proxy else None
        try:
            resp = session.get(url, **kw)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if proxy is not None:
                proxy_pool.mark_failure(proxy)
            if stats is not None:
                stats.network_errors += 1
            continue

        if is_block_status(getattr(resp, "status_code", None)):
            last_resp = resp
            if proxy is not None:
                proxy_pool.mark_failure(proxy)
            if stats is not None:
                stats.last_blocked_at = datetime.now(UTC)
                stats.block_count += 1
            continue

        # Success path.
        if proxy is not None:
            proxy_pool.mark_success(proxy)
        return resp

    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    # Shouldn't reach here (max_retries clamped to ≥1).
    raise RuntimeError(f"proxied_get_sync exhausted retries on {url}")
