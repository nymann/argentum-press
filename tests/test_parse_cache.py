"""Tests for the opt-in disk parse cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from argentum_press import parse_cache
from argentum_press.parser import ParseResult, ast as ast_module


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the cache to a temp dir and turn it on for the test."""
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE", "1")
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE_DIR", str(tmp_path))
    return tmp_path


def _bird() -> dict[str, str]:
    """A trivially-passing card used so we don't pay the real parse cost twice."""
    return {"name": "Test Bird", "oracle_text": "Flying"}


def _file_count(root: Path, suffix: str) -> int:
    return sum(1 for _ in root.rglob(f"*{suffix}"))


def test_passthrough_when_env_var_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGENTUM_PARSE_CACHE", raising=False)
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE_DIR", str(tmp_path))

    calls: list[dict] = []
    sentinel = ParseResult(ast=ast_module.Card())

    def fake_parse(card: dict) -> ParseResult:
        calls.append(card)
        return sentinel

    monkeypatch.setattr("argentum_press.parser.parse", fake_parse)
    monkeypatch.setattr("argentum_press.parse_cache.parse", fake_parse, raising=False)

    result = parse_cache.cached_parse(_bird())
    assert result is sentinel
    assert len(calls) == 1
    # Cache directory should not have been created when disabled.
    assert _file_count(tmp_path, ".pkl") == 0


def test_miss_then_hit(cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_parse(card: dict) -> ParseResult:
        nonlocal calls
        calls += 1
        return ParseResult(ast=ast_module.Card())

    monkeypatch.setattr("argentum_press.parser.parse", fake_parse)

    parse_cache.cached_parse(_bird())  # miss
    parse_cache.cached_parse(_bird())  # hit
    parse_cache.cached_parse(_bird())  # hit
    assert calls == 1
    assert _file_count(cache_dir, ".pkl") == 1
    assert _file_count(cache_dir, ".meta") == 1


def test_different_cards_get_different_entries(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_parse(card: dict) -> ParseResult:
        return ParseResult(ast=ast_module.Card())

    monkeypatch.setattr("argentum_press.parser.parse", fake_parse)

    parse_cache.cached_parse({"name": "A", "oracle_text": "Flying"})
    parse_cache.cached_parse({"name": "B", "oracle_text": "Flying"})
    parse_cache.cached_parse({"name": "A", "oracle_text": "Reach"})
    assert _file_count(cache_dir, ".pkl") == 3


def test_invalidate_label_removes_only_matching_entries(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argentum_press.parser.transformer import ParseError

    def fake_parse(card: dict) -> ParseResult:
        text = card.get("oracle_text", "")
        if text == "BROKEN":
            return ParseResult(
                error=ParseError(kind="incomplete", message="unmodeled-rule:foo")
            )
        if text == "ALSO BROKEN":
            return ParseResult(
                error=ParseError(kind="incomplete", message="unmodeled-rule:bar")
            )
        return ParseResult(ast=ast_module.Card())

    monkeypatch.setattr("argentum_press.parser.parse", fake_parse)

    parse_cache.cached_parse({"name": "X", "oracle_text": "Flying"})
    parse_cache.cached_parse({"name": "Y", "oracle_text": "BROKEN"})
    parse_cache.cached_parse({"name": "Z", "oracle_text": "BROKEN"})
    parse_cache.cached_parse({"name": "W", "oracle_text": "ALSO BROKEN"})
    assert _file_count(cache_dir, ".pkl") == 4

    removed = parse_cache.invalidate_label("unmodeled-rule:foo")
    assert removed == 2
    assert _file_count(cache_dir, ".pkl") == 2  # Flying + ALSO BROKEN remain


def test_invalidate_label_on_empty_cache_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE", "1")
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE_DIR", str(tmp_path / "nonexistent"))
    assert parse_cache.invalidate_label("anything") == 0


def test_clear_wipes_everything(cache_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_parse(card: dict) -> ParseResult:
        return ParseResult(ast=ast_module.Card())

    monkeypatch.setattr("argentum_press.parser.parse", fake_parse)
    parse_cache.cached_parse(_bird())
    assert _file_count(cache_dir, ".pkl") == 1

    parse_cache.clear()
    assert not cache_dir.exists() or _file_count(cache_dir, ".pkl") == 0


def test_corrupt_pickle_falls_through(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbled cache file should not poison the loop — re-parse and replace."""
    def fake_parse(card: dict) -> ParseResult:
        return ParseResult(ast=ast_module.Card())

    monkeypatch.setattr("argentum_press.parser.parse", fake_parse)
    parse_cache.cached_parse(_bird())
    pkl = next(cache_dir.rglob("*.pkl"))
    pkl.write_bytes(b"\x00\x01garbage")  # corrupt
    # Should not raise.
    result = parse_cache.cached_parse(_bird())
    assert result.ok
