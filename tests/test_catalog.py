"""Catalog tests use pytest-httpx to stub Scryfall and tmp_path for the cache
directory so we never touch the user's real ~/.cache/scryfall."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from argentum_press.catalog import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_HEADERS,
    ScryfallCatalog,
    ScryfallError,
)


def _make_client() -> httpx.Client:
    return httpx.Client(headers=DEFAULT_HEADERS, timeout=5.0)


def _add_set_meta(httpx_mock: HTTPXMock, code: str, released_at: str) -> None:
    httpx_mock.add_response(
        url=f"https://api.scryfall.com/sets/{code}",
        json={"code": code, "released_at": released_at, "set_type": "expansion"},
    )


def _add_search_page(
    httpx_mock: HTTPXMock,
    url: str,
    cards: list[dict[str, object]],
    *,
    next_page: str | None = None,
) -> None:
    httpx_mock.add_response(
        url=url,
        json={
            "data": cards,
            "has_more": next_page is not None,
            "next_page": next_page,
        },
    )


def test_first_run_fetches_then_writes_cache(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    _add_set_meta(httpx_mock, "por", "1997-05-01")
    _add_search_page(
        httpx_mock,
        "https://api.scryfall.com/cards/search?order=set&q=set%3Apor",
        [{"name": "Archangel"}, {"name": "Armored Pegasus"}],
    )
    with ScryfallCatalog(client=_make_client(), cache_dir=tmp_path) as catalog:
        cards = catalog.fetch("por")
    assert [c["name"] for c in cards] == ["Archangel", "Armored Pegasus"]
    assert catalog.last_cache_state is not None
    assert catalog.last_cache_state.source == "miss"

    cache_file = tmp_path / "por.cards.json"
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text())
    assert payload["_v"] == CACHE_SCHEMA_VERSION
    assert payload["released_at"] == "1997-05-01"
    assert len(payload["cards"]) == 2


def test_second_run_uses_fresh_cache_without_http(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    (tmp_path / "por.cards.json").write_text(
        json.dumps(
            {
                "_v": CACHE_SCHEMA_VERSION,
                "released_at": "1997-05-01",
                "cards": [{"name": "Cached"}],
            }
        )
    )
    # No httpx mock added: any HTTP request would raise.
    with ScryfallCatalog(client=_make_client(), cache_dir=tmp_path) as catalog:
        cards = catalog.fetch("por")
    assert [c["name"] for c in cards] == ["Cached"]
    assert catalog.last_cache_state is not None
    assert catalog.last_cache_state.hit


def test_recently_released_set_refetches_even_with_cache(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    recently_released = (date.today() - timedelta(days=3)).isoformat()
    (tmp_path / "new.cards.json").write_text(
        json.dumps(
            {
                "_v": CACHE_SCHEMA_VERSION,
                "released_at": recently_released,
                "cards": [{"name": "Stale"}],
            }
        )
    )
    _add_set_meta(httpx_mock, "new", recently_released)
    _add_search_page(
        httpx_mock,
        "https://api.scryfall.com/cards/search?order=set&q=set%3Anew",
        [{"name": "Fresh"}],
    )
    with ScryfallCatalog(client=_make_client(), cache_dir=tmp_path) as catalog:
        cards = catalog.fetch("new")
    assert [c["name"] for c in cards] == ["Fresh"]
    assert catalog.last_cache_state is not None
    assert catalog.last_cache_state.source == "hit-stale-refetched"


def test_force_refresh_bypasses_cache(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    (tmp_path / "por.cards.json").write_text(
        json.dumps(
            {
                "_v": CACHE_SCHEMA_VERSION,
                "released_at": "1997-05-01",
                "cards": [{"name": "Cached"}],
            }
        )
    )
    _add_set_meta(httpx_mock, "por", "1997-05-01")
    _add_search_page(
        httpx_mock,
        "https://api.scryfall.com/cards/search?order=set&q=set%3Apor",
        [{"name": "Refetched"}],
    )
    with ScryfallCatalog(
        client=_make_client(), cache_dir=tmp_path, force_refresh=True
    ) as catalog:
        cards = catalog.fetch("por")
    assert [c["name"] for c in cards] == ["Refetched"]
    assert catalog.last_cache_state is not None
    assert catalog.last_cache_state.source == "forced-refresh"


def test_pagination_follows_next_page(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    _add_set_meta(httpx_mock, "blb", "1900-01-01")
    next_url = "https://api.scryfall.com/cards/search?page=2&order=set&q=set%3Ablb"
    _add_search_page(
        httpx_mock,
        "https://api.scryfall.com/cards/search?order=set&q=set%3Ablb",
        [{"name": "A"}],
        next_page=next_url,
    )
    _add_search_page(
        httpx_mock,
        next_url,
        [{"name": "B"}, {"name": "C"}],
    )
    with ScryfallCatalog(client=_make_client(), cache_dir=tmp_path) as catalog:
        cards = catalog.fetch("blb")
    assert [c["name"] for c in cards] == ["A", "B", "C"]


def test_non_200_raises_when_no_cache_available(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        url="https://api.scryfall.com/sets/nope",
        status_code=404,
        json={"object": "error", "details": "no cards found"},
    )
    with ScryfallCatalog(client=_make_client(), cache_dir=tmp_path) as catalog, pytest.raises(
        ScryfallError
    ):
        catalog.fetch("nope")


def test_falls_back_to_stale_cache_on_http_failure(
    httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    recently_released = (date.today() - timedelta(days=2)).isoformat()
    (tmp_path / "spoiled.cards.json").write_text(
        json.dumps(
            {
                "_v": CACHE_SCHEMA_VERSION,
                "released_at": recently_released,
                "cards": [{"name": "StaleButUsable"}],
            }
        )
    )
    httpx_mock.add_response(
        url="https://api.scryfall.com/sets/spoiled",
        status_code=503,
        text="upstream blip",
    )
    with ScryfallCatalog(client=_make_client(), cache_dir=tmp_path) as catalog:
        cards = catalog.fetch("spoiled")
    assert [c["name"] for c in cards] == ["StaleButUsable"]
    assert catalog.last_cache_state is not None
    assert catalog.last_cache_state.source == "hit-stale-refetched"


def test_sends_required_scryfall_headers(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    _add_set_meta(httpx_mock, "por", "1997-05-01")
    httpx_mock.add_response(
        url="https://api.scryfall.com/cards/search?order=set&q=set%3Apor",
        match_headers={"User-Agent": "argentum-press/0.1", "Accept": "application/json"},
        json={"data": [], "has_more": False, "next_page": None},
    )
    with ScryfallCatalog(client=_make_client(), cache_dir=tmp_path) as catalog:
        catalog.fetch("por")
