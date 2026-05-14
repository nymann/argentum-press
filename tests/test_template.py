"""Pins the rendered shape against the conventions used by real argentum cards.

Reference: argentum-engine/mtg-sets/.../blb/cards/{ShoreUp,AzureBeastbinder,
BrightbladeStoat}.kt — package, imports, val PascalName, top-level field
order, metadata block.
"""

from __future__ import annotations

from typing import Any

from argentum_press.template import (
    basic_land_subtype,
    is_basic_land,
    render,
    render_basic_lands,
)


def test_keyworded_creature_renders_in_real_dsl_shape() -> None:
    card = {
        "name": "Brightblade Stoat",
        "mana_cost": "{1}{W}",
        "type_line": "Creature — Weasel Soldier",
        "power": "2",
        "toughness": "2",
        "oracle_text": "First strike, lifelink",
        "rarity": "uncommon",
        "collector_number": "4",
        "color_identity": ["W"],
        "artist": "Lius Lasahido",
        "flavor_text": "Brightblades are trained to mind the sun's position.",
        "image_uris": {"normal": "https://example/stoat.jpg"},
    }
    body = "keywords(Keyword.FIRST_STRIKE, Keyword.LIFELINK)"
    out = render(card, body, "blb")
    assert out.startswith("package com.wingedsheep.mtg.sets.definitions.blb.cards\n")
    assert "import com.wingedsheep.sdk.core.Keyword" in out
    assert "import com.wingedsheep.sdk.dsl.card" in out
    assert "import com.wingedsheep.sdk.model.Rarity" in out
    assert "import com.wingedsheep.sdk.dsl.Effects" not in out
    assert 'val BrightbladeStoat = card("Brightblade Stoat") {' in out
    assert '    manaCost = "{1}{W}"' in out
    assert '    colorIdentity = "W"' in out
    assert "    power = 2" in out
    assert "    toughness = 2" in out
    assert "    keywords(Keyword.FIRST_STRIKE, Keyword.LIFELINK)" in out
    assert "    metadata {" in out
    assert "        rarity = Rarity.UNCOMMON" in out
    assert '        collectorNumber = "4"' in out


def test_omits_null_metadata_fields() -> None:
    card = {
        "name": "Lightning Bolt",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "power": None,
        "toughness": None,
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
        "rarity": "common",
        "collector_number": "1",
        "color_identity": ["R"],
        "artist": None,
        "flavor_text": None,
        "image_uris": None,
    }
    out = render(card, "spell { effect = Effects.DealDamage(3, target(\"any\", Targets.Any)) }", "lea")
    assert "    power =" not in out
    assert "    toughness =" not in out
    assert "artist =" not in out
    assert "flavorText =" not in out
    assert "imageUri =" not in out


def test_pascal_case_strips_apostrophes() -> None:
    card = {
        "name": "Sazacap's Brew",
        "mana_cost": "{1}{R}",
        "type_line": "Instant",
        "oracle_text": "",
        "rarity": "rare",
        "collector_number": "1",
        "color_identity": ["R"],
    }
    out = render(card, "", "blb")
    assert 'val SazacapsBrew = card("Sazacap\\\'s Brew") {' in out or \
           'val SazacapsBrew = card("Sazacap\'s Brew") {' in out


def test_escapes_quotes_and_newlines() -> None:
    card = {
        "name": "Q",
        "mana_cost": "{W}",
        "type_line": "Instant",
        "oracle_text": 'Line one.\nLine two with "quotes".',
        "rarity": "common",
        "collector_number": "1",
        "color_identity": ["W"],
    }
    out = render(card, "", "tst")
    assert 'oracleText = "Line one.\\nLine two with \\"quotes\\"."' in out


def test_empty_color_identity_omits_field() -> None:
    card: dict[str, Any] = {
        "name": "Colorless",
        "mana_cost": "{1}",
        "type_line": "Artifact",
        "oracle_text": "",
        "rarity": "common",
        "collector_number": "1",
        "color_identity": [],
    }
    out = render(card, "", "tst")
    assert "colorIdentity =" not in out


def test_unmapped_rarity_skipped() -> None:
    card: dict[str, Any] = {
        "name": "Weird",
        "mana_cost": "{1}",
        "type_line": "Instant",
        "oracle_text": "",
        "rarity": "weird",
        "collector_number": "1",
        "color_identity": [],
    }
    out = render(card, "", "tst")
    assert "rarity =" not in out


def test_braces_balance() -> None:
    card: dict[str, Any] = {
        "name": "Empty",
        "mana_cost": "{1}",
        "type_line": "Artifact",
        "oracle_text": "",
        "rarity": "common",
        "collector_number": "1",
        "color_identity": [],
    }
    out = render(card, "", "tst")
    assert out.count("{") == out.count("}")


# ---- basic-land detection ----


def test_is_basic_land_matches_basic_and_snow_basic() -> None:
    assert is_basic_land({"name": "Plains", "type_line": "Basic Land — Plains"})
    assert is_basic_land(
        {"name": "Snow-Covered Forest", "type_line": "Basic Snow Land — Forest"}
    )
    assert is_basic_land({"name": "Wastes", "type_line": "Basic Land — Wastes"})


def test_is_basic_land_rejects_non_basics() -> None:
    assert not is_basic_land({"name": "Krosan Verge", "type_line": "Land"})
    assert not is_basic_land(
        {"name": "Lotus Field", "type_line": "Land"}
    )
    assert not is_basic_land(
        {"name": "Counterspell", "type_line": "Instant"}
    )


def test_basic_land_subtype_extracts_after_dash() -> None:
    assert basic_land_subtype({"type_line": "Basic Land — Forest"}) == "Forest"
    assert (
        basic_land_subtype({"type_line": "Basic Snow Land — Mountain"}) == "Mountain"
    )
    assert basic_land_subtype({"type_line": "Land"}) is None


# ---- basic-land file rendering ----


def _basic(subtype: str, collector: str, artist: str = "A") -> dict[str, Any]:
    return {
        "name": subtype,
        "type_line": f"Basic Land — {subtype}",
        "collector_number": collector,
        "artist": artist,
        "image_uris": {"normal": f"https://example/{subtype.lower()}-{collector}.jpg"},
    }


def test_render_basic_lands_uses_basicLand_dsl_and_listOf_aggregator() -> None:
    cards = [
        _basic("Plains", "262"),
        _basic("Island", "266"),
        _basic("Forest", "278"),
    ]
    out = render_basic_lands("blb", "Bloomburrow", cards)
    assert out.startswith("package com.wingedsheep.mtg.sets.definitions.blb.cards\n")
    assert "import com.wingedsheep.sdk.dsl.basicLand" in out
    # No `card("Plains")` — basics must NOT use the regular DSL.
    assert 'card("Plains")' not in out
    assert 'val BloomburrowPlains262 = basicLand("Plains") {' in out
    assert 'val BloomburrowIsland266 = basicLand("Island") {' in out
    assert 'val BloomburrowForest278 = basicLand("Forest") {' in out
    assert '    collectorNumber = "262"' in out
    assert "val BloomburrowBasicLands = listOf(" in out
    assert "    BloomburrowPlains262," in out


def test_render_basic_lands_sorts_by_type_then_collector() -> None:
    cards = [
        _basic("Forest", "281"),
        _basic("Plains", "262"),
        _basic("Forest", "278"),
        _basic("Plains", "264"),
    ]
    out = render_basic_lands("blb", "Bloomburrow", cards)
    plains_262 = out.index("BloomburrowPlains262")
    plains_264 = out.index("BloomburrowPlains264")
    forest_278 = out.index("BloomburrowForest278")
    forest_281 = out.index("BloomburrowForest281")
    assert plains_262 < plains_264 < forest_278 < forest_281


def test_render_basic_lands_handles_promo_collector_chars() -> None:
    out = render_basic_lands(
        "tst", "Test", [_basic("Plains", "262★")]
    )
    # Identifier must be alnum-only; the original collector string is preserved
    # in the `collectorNumber = "..."` field for the engine to see.
    assert 'val TestPlains262 = basicLand("Plains") {' in out
    assert '    collectorNumber = "262★"' in out
