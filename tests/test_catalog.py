"""Catalog tests use pytest-httpx to stub Scryfall without hitting the network."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from argentum_press.catalog import (
    DEFAULT_HEADERS,
    ScryfallCatalog,
    ScryfallError,
)


def _make_client() -> httpx.Client:
    # Mirror ScryfallCatalog's own client config so pytest-httpx intercepts.
    return httpx.Client(headers=DEFAULT_HEADERS, timeout=5.0)


def test_single_page_returns_every_card(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.scryfall.com/cards/search?order=set&q=set%3Apor",
        json={
            "data": [{"name": "Archangel"}, {"name": "Armored Pegasus"}],
            "has_more": False,
            "next_page": None,
        },
    )
    with ScryfallCatalog(client=_make_client()) as catalog:
        cards = catalog.fetch("por")
    assert [c["name"] for c in cards] == ["Archangel", "Armored Pegasus"]


def test_follows_next_page_until_has_more_false(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.scryfall.com/cards/search?order=set&q=set%3Ablb",
        json={
            "data": [{"name": "A"}],
            "has_more": True,
            "next_page": "https://api.scryfall.com/cards/search?page=2&order=set&q=set%3Ablb",
        },
    )
    httpx_mock.add_response(
        url="https://api.scryfall.com/cards/search?page=2&order=set&q=set%3Ablb",
        json={
            "data": [{"name": "B"}, {"name": "C"}],
            "has_more": False,
            "next_page": None,
        },
    )
    with ScryfallCatalog(client=_make_client()) as catalog:
        cards = catalog.fetch("blb")
    assert [c["name"] for c in cards] == ["A", "B", "C"]


def test_sends_required_scryfall_headers(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.scryfall.com/cards/search?order=set&q=set%3Apor",
        match_headers={"User-Agent": "argentum-press/0.1", "Accept": "application/json"},
        json={"data": [], "has_more": False, "next_page": None},
    )
    with ScryfallCatalog(client=_make_client()) as catalog:
        catalog.fetch("por")


def test_non_200_raises_with_body_for_diagnosis(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.scryfall.com/cards/search?order=set&q=set%3Anope",
        status_code=404,
        json={"object": "error", "details": "no cards found"},
    )
    with ScryfallCatalog(client=_make_client()) as catalog, pytest.raises(ScryfallError) as info:
        catalog.fetch("nope")
    assert "404" in str(info.value)
    assert "no cards found" in str(info.value)
