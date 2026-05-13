"""Pins the rendered shape against the conventions used by real argentum cards.

Reference: argentum-engine/mtg-sets/.../blb/cards/{ShoreUp,AzureBeastbinder,
BrightbladeStoat}.kt — package, imports, val PascalName, top-level field
order, metadata block.
"""

from __future__ import annotations

from typing import Any

from argentum_press.template import render


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
