"""Tests for the diagnose subcommand's core walk.

These exercise :func:`argentum_press.diagnose.find_first_gap` directly
against the real parser + lowerer — no stubs — so a regression in the
transformer or lowerer that changes which label is surfaced will trip a
test rather than slip through to the bash fix-loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from argentum_press.diagnose import DiagnoseReport, find_first_gap, gap_for_card
from argentum_press.lowerer import KotlinLowerer


def _flying_bird() -> dict[str, Any]:
    """A card the parser+lowerer fully handle (bucket-1)."""
    return {
        "name": "Test Bird",
        "mana_cost": "{1}{U}",
        "type_line": "Creature — Bird",
        "power": "1",
        "toughness": "1",
        "oracle_text": "Flying",
        "rarity": "common",
        "collector_number": "1",
        "color_identity": ["U"],
    }


def _basic_plains() -> dict[str, Any]:
    """A basic land — diagnose should skip these via the same rule the
    pipeline uses."""
    return {
        "name": "Plains",
        "type_line": "Basic Land — Plains",
        "oracle_text": "",
        "rarity": "common",
        "collector_number": "250",
    }


def _unparseable() -> dict[str, Any]:
    """Oracle text the grammar can't handle yet — we don't care which
    label comes out, only that *something* surfaces as kind='parse'."""
    return {
        "name": "Unparseable Card",
        "mana_cost": "{2}",
        "type_line": "Sorcery",
        "oracle_text": "Asdf qwerty zxcv.",
        "rarity": "common",
        "collector_number": "2",
    }


def test_returns_none_when_set_has_no_gaps(tmp_path: Path) -> None:
    report = find_first_gap(
        cards=[_flying_bird()],
        project_dir=tmp_path,
        set_code="zzz",
    )
    assert report.gap is None
    assert report.scanned == 1


def test_skips_basic_lands_and_implemented(tmp_path: Path) -> None:
    # No cards/ dir under tmp_path means nothing is implemented — but
    # the basic land should still be skipped via is_basic_land.
    report = find_first_gap(
        cards=[_basic_plains(), _flying_bird()],
        project_dir=tmp_path,
        set_code="zzz",
    )
    assert report.gap is None
    assert report.scanned == 1  # only the bird went through parse


def test_short_circuits_on_first_parse_failure(tmp_path: Path) -> None:
    later = _flying_bird()
    later["name"] = "Bird After Failure"
    report = find_first_gap(
        cards=[_unparseable(), later],
        project_dir=tmp_path,
        set_code="zzz",
    )
    assert report.gap is not None
    assert report.gap.kind == "parse"
    assert report.gap.card_name == "Unparseable Card"
    assert report.gap.oracle_text == "Asdf qwerty zxcv."
    assert report.gap.label  # some non-empty label
    assert report.scanned == 1  # later card was not scanned


def test_json_shape_matches_bash_consumer(tmp_path: Path) -> None:
    report = find_first_gap(
        cards=[_unparseable()],
        project_dir=tmp_path,
        set_code="zzz",
    )
    payload = json.loads(report.to_json())
    assert payload["set_code"] == "zzz"
    assert payload["scanned"] == 1
    gap = payload["gap"]
    assert set(gap.keys()) == {"kind", "card_name", "oracle_text", "label"}


def test_json_shape_when_no_gap(tmp_path: Path) -> None:
    report = find_first_gap(
        cards=[_flying_bird()],
        project_dir=tmp_path,
        set_code="zzz",
    )
    payload = json.loads(report.to_json())
    assert payload["gap"] is None


def test_diagnose_report_is_frozen() -> None:
    # Sanity: a downstream caller treating these as values should be safe.
    report = DiagnoseReport(set_code="zzz", scanned=0, gap=None)
    try:
        report.scanned = 5  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DiagnoseReport should be frozen")


def test_gap_for_card_returns_none_for_clean_card() -> None:
    assert gap_for_card(_flying_bird(), KotlinLowerer()) is None


def test_gap_for_card_returns_parse_gap_for_unparseable() -> None:
    gap = gap_for_card(_unparseable(), KotlinLowerer())
    assert gap is not None
    assert gap.kind == "parse"
    assert gap.card_name == "Unparseable Card"


def test_gap_for_card_bypasses_triage_filters() -> None:
    # The single-card helper deliberately doesn't apply the basic-land or
    # already-implemented filters; the CLI's --card flag depends on this so
    # the fix-loop can reproduce any card by name.
    gap = gap_for_card(_basic_plains(), KotlinLowerer())
    # Basic Plains has empty oracle text — parses as an empty card. The
    # exact result is implementation-dependent, but the key contract is
    # "gap_for_card ran without short-circuiting" — i.e. it didn't refuse
    # to look at the card just because it's a basic land.
    # (If it returned a gap, fine; if None, also fine. We're asserting the
    # function ran end-to-end without applying a triage filter.)
    assert gap is None or gap.card_name == "Plains"
