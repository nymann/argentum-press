"""Scryfall card catalog.

Fetches every printing of a given set as a list of raw Scryfall card dicts
(the same shape mtgcompiler.parse() consumes). Pagination follows next_page
until has_more is false.

Scryfall requires User-Agent + Accept headers on every request and returns
HTTP 400 with a clear message otherwise.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_BASE = "https://api.scryfall.com"
DEFAULT_HEADERS = {
    "User-Agent": "argentum-press/0.1",
    "Accept": "application/json",
}


class ScryfallError(RuntimeError):
    """Scryfall returned a non-2xx response we don't know how to recover from."""


class ScryfallCatalog:
    """Fetches Scryfall card data; one instance per session is fine.

    The catalog returns raw dicts because mtgcompiler.parse() takes the
    Scryfall shape directly — wrapping each card in a custom dataclass would
    just be ceremony, since we don't transform them on this side.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(headers=DEFAULT_HEADERS, timeout=30.0)
        self._base_url = base_url.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ScryfallCatalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch(self, set_code: str) -> list[dict[str, Any]]:
        """Return every card in the given set.

        Uses /cards/search?q=set:<code>&order=set; the /sets/<code>/cards
        endpoint Scryfall used to expose has been removed.
        """
        query = quote(f"set:{set_code}")
        url: str | None = f"{self._base_url}/cards/search?order=set&q={query}"
        cards: list[dict[str, Any]] = []
        while url:
            response = self._client.get(url)
            if response.status_code != 200:
                raise ScryfallError(
                    f"Scryfall returned {response.status_code} for {url}: {response.text}"
                )
            payload = response.json()
            cards.extend(payload.get("data", []))
            url = payload.get("next_page") if payload.get("has_more") else None
        return cards
