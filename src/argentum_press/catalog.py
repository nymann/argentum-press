"""Scryfall card catalog with on-disk cache.

Fetches every printing of a given set as a list of raw Scryfall card dicts
(the same shape mtgcompiler.parse() consumes). Pagination follows next_page
until has_more is false.

Cache: ~/.cache/scryfall/<code>.cards.json holds the full payloads plus the
set's `released_at`. A cached payload is "fresh" once released_at is more
than 30 days in the past — MTG sets are frozen after release. Sets still
in spoiler season re-fetch on every run. Caller can force a re-fetch by
passing `force_refresh=True`.

Scryfall requires User-Agent + Accept headers on every request and returns
HTTP 400 otherwise.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_BASE = "https://api.scryfall.com"
DEFAULT_HEADERS = {
    "User-Agent": "argentum-press/0.1",
    "Accept": "application/json",
}
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "scryfall"
CACHE_SCHEMA_VERSION = 1
REFRESH_WINDOW_DAYS = 30


class ScryfallError(RuntimeError):
    """Scryfall returned a non-2xx response we don't know how to recover from."""


class CacheState:
    """Result of a cache lookup, attached to a fetch so the reporter can
    tell the user 'cache: hit (fresh)' / 'cache: refreshed' / 'cache: miss'.
    """

    __slots__ = ("source",)

    def __init__(self, source: str) -> None:
        # one of: "hit-fresh", "hit-stale-refetched", "miss", "forced-refresh"
        self.source = source

    @property
    def hit(self) -> bool:
        return self.source == "hit-fresh"


class ScryfallCatalog:
    def __init__(
        self,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE,
        cache_dir: Path | None = DEFAULT_CACHE_DIR,
        *,
        force_refresh: bool = False,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(headers=DEFAULT_HEADERS, timeout=30.0)
        self._base_url = base_url.rstrip("/")
        self._cache_dir = cache_dir
        self._force_refresh = force_refresh
        self.last_cache_state: CacheState | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ScryfallCatalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch(self, set_code: str) -> list[dict[str, Any]]:
        cached = None if self._force_refresh else self._read_cache(set_code)
        if cached is not None and _is_fresh(cached):
            self.last_cache_state = CacheState("hit-fresh")
            return list(cached["cards"])

        try:
            payload = self._fetch_fresh(set_code)
        except ScryfallError:
            if cached is not None:
                # Network/upstream failure but we have a stale copy — better
                # than nothing for the pipeline to keep going.
                self.last_cache_state = CacheState("hit-stale-refetched")
                return list(cached["cards"])
            raise

        self._write_cache(set_code, payload)
        if self._force_refresh:
            self.last_cache_state = CacheState("forced-refresh")
        elif cached is not None:
            self.last_cache_state = CacheState("hit-stale-refetched")
        else:
            self.last_cache_state = CacheState("miss")
        return list(payload["cards"])

    def _fetch_fresh(self, set_code: str) -> dict[str, Any]:
        # Set metadata for `released_at`, which gates cache freshness.
        meta = self._http_get(f"{self._base_url}/sets/{set_code}")
        released_at = meta.get("released_at")

        cards: list[dict[str, Any]] = []
        query = quote(f"set:{set_code}")
        url: str | None = f"{self._base_url}/cards/search?order=set&q={query}"
        while url:
            page = self._http_get(url)
            cards.extend(page.get("data", []))
            url = page.get("next_page") if page.get("has_more") else None
        return {
            "_v": CACHE_SCHEMA_VERSION,
            "released_at": released_at,
            "set_type": meta.get("set_type"),
            "cards": cards,
        }

    def _http_get(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        if response.status_code != 200:
            raise ScryfallError(
                f"Scryfall returned {response.status_code} for {url}: {response.text}"
            )
        return response.json()  # type: ignore[no-any-return]

    def _cache_file(self, set_code: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"{set_code.lower()}.cards.json"

    def _read_cache(self, set_code: str) -> dict[str, Any] | None:
        path = self._cache_file(set_code)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if payload.get("_v") != CACHE_SCHEMA_VERSION:
            return None
        return payload  # type: ignore[no-any-return]

    def _write_cache(self, set_code: str, payload: dict[str, Any]) -> None:
        path = self._cache_file(set_code)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _is_fresh(payload: dict[str, Any]) -> bool:
    released = payload.get("released_at")
    if not released:
        return False
    try:
        released_date = date.fromisoformat(released)
    except ValueError:
        return False
    return date.today() - released_date >= timedelta(days=REFRESH_WINDOW_DAYS)
