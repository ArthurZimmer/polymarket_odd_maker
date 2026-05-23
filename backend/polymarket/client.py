"""Thin async client over Polymarket's public Gamma API.

For Etapa 3 we only need read-only event/market discovery, so this is a plain
HTTPX wrapper. The CLOB WebSocket + trading client (py-clob-client) is added
in Etapa 4/Etapa 9 where needed.
"""
from __future__ import annotations

from typing import Any

import httpx

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DEFAULT_PAGE_SIZE = 500
MAX_PAGES = 4  # cap discovery at ~2k events to keep the request budget sane


class PolymarketGammaClient:
    def __init__(self, base_url: str = GAMMA_API_BASE, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PolymarketGammaClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def fetch_sports_events(
        self,
        *,
        max_pages: int = MAX_PAGES,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """Fetch all active, non-closed events tagged 'sports', with inline tags.

        Paginated. Stops when a page returns fewer than `page_size` records
        (or `max_pages` is hit).
        """
        out: list[dict[str, Any]] = []
        for page in range(max_pages):
            params: dict[str, Any] = {
                "closed": "false",
                "active": "true",
                "tag_slug": "sports",
                "include_tag": "true",
                "limit": page_size,
                "offset": page * page_size,
                "order": "endDate",
                "ascending": "true",
            }
            resp = await self._client.get("/events", params=params)
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
        return out
