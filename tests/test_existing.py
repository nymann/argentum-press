"""Tests for the argentum-engine scanner. Each test builds a fake project
layout in tmp_path so we don't depend on a real argentum checkout."""

from __future__ import annotations

from pathlib import Path

import pytest

from argentum_press.existing import (
    cards_dir,
    front_face,
    implemented_cards_in_set,
)


def _make_card_file(target: Path, kt_body: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(kt_body)


def test_returns_empty_set_when_set_directory_does_not_exist(tmp_path: Path) -> None:
    assert implemented_cards_in_set(tmp_path, "blb") == set()


def test_picks_up_card_dsl_call(tmp_path: Path) -> None:
    _make_card_file(
        cards_dir(tmp_path, "blb") / "BrightbladeStoat.kt",
        """
        val BrightbladeStoat = card("Brightblade Stoat") {
            manaCost = "{1}{W}"
        }
        """,
    )
    assert implemented_cards_in_set(tmp_path, "blb") == {"Brightblade Stoat"}


def test_picks_up_basic_land_dsl(tmp_path: Path) -> None:
    _make_card_file(
        cards_dir(tmp_path, "blb") / "Plains.kt",
        'val Plains = basicLand("Plains") { }\n',
    )
    assert implemented_cards_in_set(tmp_path, "blb") == {"Plains"}


def test_picks_up_printing_rows(tmp_path: Path) -> None:
    _make_card_file(
        cards_dir(tmp_path, "blb") / "Reprints.kt",
        """
        listOf(
            Printing(name = "Counterspell", set = "blb", number = "42"),
            Printing(name = "Shock", set = "blb", number = "43"),
        )
        """,
    )
    assert implemented_cards_in_set(tmp_path, "blb") == {"Counterspell", "Shock"}


def test_normalises_dfc_names_to_front_face(tmp_path: Path) -> None:
    _make_card_file(
        cards_dir(tmp_path, "blb") / "DayUnto.kt",
        'val DayUntoNight = card("Day // Night") { }\n',
    )
    # The Scryfall canonical also includes the back face; argentum's convention
    # is to register under the full name. We compare on front-face only so the
    # missing-set logic in the pipeline can subtract cleanly.
    assert implemented_cards_in_set(tmp_path, "blb") == {"Day"}


def test_multiple_files_aggregate(tmp_path: Path) -> None:
    _make_card_file(
        cards_dir(tmp_path, "blb") / "A.kt",
        'val A = card("A") { }\n',
    )
    _make_card_file(
        cards_dir(tmp_path, "blb") / "B.kt",
        'val B = card("B") { }\n',
    )
    assert implemented_cards_in_set(tmp_path, "blb") == {"A", "B"}


def test_other_set_directories_are_ignored(tmp_path: Path) -> None:
    _make_card_file(
        cards_dir(tmp_path, "blb") / "Bird.kt",
        'val Bird = card("Bird") { }\n',
    )
    _make_card_file(
        cards_dir(tmp_path, "ond") / "Other.kt",
        'val Other = card("Other") { }\n',
    )
    assert implemented_cards_in_set(tmp_path, "blb") == {"Bird"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Lightning Bolt", "Lightning Bolt"),
        ("Fire // Ice", "Fire"),
        ("Akki Lavarunner // Tok-Tok, Volcano-Born", "Akki Lavarunner"),
    ],
)
def test_front_face_strips_back_after_double_slash(raw: str, expected: str) -> None:
    assert front_face(raw) == expected
