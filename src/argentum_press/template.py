"""Render a Scryfall card + an emitted body string into a complete .kt file.

Rules cribbed from /tmp/verify_metadata.py and the real argentum-engine
cards (mtg-sets/.../<set>/cards/*.kt):

- Fields Scryfall returns as null are omitted from the metadata block.
- Power / toughness are emitted as Int when parseable; non-integer ("*",
  null) is skipped.
- Rarity maps lowercase Scryfall string -> Rarity.<X>.
- The val identifier is PascalCase of the display name, dropping
  apostrophes ("Sazacap's Brew" -> "SazacapsBrew") so they don't break
  the word.
- Imports are inferred from tokens that appear in the body, so a vanilla
  creature doesn't import Effects/Targets/Triggers.
"""

from __future__ import annotations

from typing import Any

_RARITY: dict[str, str] = {
    "common": "Rarity.COMMON",
    "uncommon": "Rarity.UNCOMMON",
    "rare": "Rarity.RARE",
    "mythic": "Rarity.MYTHIC",
    "special": "Rarity.SPECIAL",
    "bonus": "Rarity.BONUS",
}

BASIC_LAND_SUBTYPES = ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes")


def is_basic_land(card: dict[str, Any]) -> bool:
    """Scryfall basics have a `type_line` starting with `Basic ` (covers both
    "Basic Land — Plains" and "Basic Snow Land — Forest") and a recognised
    basic-land subtype."""
    type_line = card.get("type_line") or ""
    if not type_line.startswith("Basic "):
        return False
    return basic_land_subtype(card) is not None


def basic_land_subtype(card: dict[str, Any]) -> str | None:
    """The basic-land subtype after the em-dash (Plains/Island/.../Wastes)."""
    type_line = card.get("type_line") or ""
    if "—" not in type_line:
        return None
    _, after = type_line.split("—", 1)
    for token in after.replace("/", " ").split():
        if token in BASIC_LAND_SUBTYPES:
            return token
    return None


def render_basic_lands(
    set_code: str,
    set_prefix: str,
    cards: list[dict[str, Any]],
) -> str:
    """Emit one combined `<Prefix>BasicLands.kt` for every basic-land printing
    in a set. Mirrors the engine's hand-authored convention (see
    `BloomburrowBasicLands.kt`): one `basicLand("<Subtype>") { ... }` per
    printing, sorted by collector number, plus an aggregating `listOf(...)`
    at the bottom."""
    pkg = f"com.wingedsheep.mtg.sets.definitions.{set_code}.cards"
    ordered = sorted(cards, key=_basic_sort_key)
    lines: list[str] = []
    lines.append(f"package {pkg}")
    lines.append("")
    lines.append("import com.wingedsheep.sdk.dsl.basicLand")
    lines.append("")
    val_names: list[str] = []
    for card in ordered:
        subtype = basic_land_subtype(card)
        if subtype is None:
            continue
        collector = card.get("collector_number") or ""
        val_name = f"{set_prefix}{subtype}{_identifier_suffix(collector)}"
        val_names.append(val_name)
        lines.append(f'val {val_name} = basicLand("{_escape(subtype)}") {{')
        if collector:
            lines.append(f'    collectorNumber = "{_escape(collector)}"')
        if card.get("artist"):
            lines.append(f'    artist = "{_escape(card["artist"])}"')
        image_uris: dict[str, Any] = card.get("image_uris") or {}
        image_uri: str | None = image_uris.get("normal")
        if image_uri:
            lines.append(f'    imageUri = "{_escape(image_uri)}"')
        lines.append("}")
        lines.append("")
    lines.append(f"val {set_prefix}BasicLands = listOf(")
    for name in val_names:
        lines.append(f"    {name},")
    lines.append(")")
    return "\n".join(lines) + "\n"


def _basic_sort_key(card: dict[str, Any]) -> tuple[int, int, str]:
    """Order by basic-land type first (Plains, Island, ...), then collector
    number. The type ordering matches the WUBRG colour wheel + Wastes that
    the engine's hand-authored files use."""
    subtype = basic_land_subtype(card) or "Wastes"
    type_index = (
        BASIC_LAND_SUBTYPES.index(subtype)
        if subtype in BASIC_LAND_SUBTYPES
        else len(BASIC_LAND_SUBTYPES)
    )
    collector = card.get("collector_number") or ""
    leading_digits = ""
    for ch in collector:
        if ch.isdigit():
            leading_digits += ch
        else:
            break
    collector_num = int(leading_digits) if leading_digits else 10_000_000
    return (type_index, collector_num, collector)


def _identifier_suffix(collector: str) -> str:
    """Identifier-safe collector suffix; drops non-alphanumerics so e.g.
    '262★' becomes '262'."""
    return "".join(ch for ch in collector if ch.isalnum())


def render(card: dict[str, Any], body: str, set_code: str) -> str:
    """Produce the full Kotlin source for one card."""
    pkg = f"com.wingedsheep.mtg.sets.definitions.{set_code}.cards"
    val_name = pascal_case(card["name"])

    imports = _required_imports(body, has_rarity=card.get("rarity") is not None)

    lines: list[str] = []
    lines.append(f"package {pkg}")
    lines.append("")
    for imp in imports:
        lines.append(f"import {imp}")
    lines.append("")
    lines.append(f'val {val_name} = card("{_escape(card["name"])}") {{')
    lines.append(f'    manaCost = "{_escape(card.get("mana_cost") or "")}"')

    color_identity = _color_identity(card)
    if color_identity:
        lines.append(f'    colorIdentity = "{color_identity}"')

    lines.append(f'    typeLine = "{_escape(card.get("type_line") or "")}"')

    power = _try_int(card.get("power"))
    toughness = _try_int(card.get("toughness"))
    if power is not None:
        lines.append(f"    power = {power}")
    if toughness is not None:
        lines.append(f"    toughness = {toughness}")

    lines.append(f'    oracleText = "{_escape(card.get("oracle_text") or "")}"')

    if body:
        lines.append("")
        for body_line in body.splitlines():
            lines.append(f"    {body_line}" if body_line else "")

    lines.append("")
    lines.append("    metadata {")
    rarity = _RARITY.get((card.get("rarity") or "").lower())
    if rarity:
        lines.append(f"        rarity = {rarity}")
    collector = card.get("collector_number")
    if collector:
        lines.append(f'        collectorNumber = "{_escape(collector)}"')
    if card.get("artist"):
        lines.append(f'        artist = "{_escape(card["artist"])}"')
    if card.get("flavor_text"):
        lines.append(f'        flavorText = "{_escape(card["flavor_text"])}"')
    image_uris: dict[str, Any] = card.get("image_uris") or {}
    image_uri: str | None = image_uris.get("normal")
    if image_uri:
        lines.append(f'        imageUri = "{_escape(image_uri)}"')
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _required_imports(body: str, *, has_rarity: bool) -> list[str]:
    needed = {"com.wingedsheep.sdk.dsl.card"}
    if has_rarity:
        needed.add("com.wingedsheep.sdk.model.Rarity")
    if "Keyword." in body:
        needed.add("com.wingedsheep.sdk.core.Keyword")
    if "Effects." in body:
        needed.add("com.wingedsheep.sdk.dsl.Effects")
    if "Targets." in body or "target(" in body:
        needed.add("com.wingedsheep.sdk.dsl.Targets")
    if "Triggers." in body:
        needed.add("com.wingedsheep.sdk.dsl.Triggers")
    return sorted(needed)


def _color_identity(card: dict[str, Any]) -> str:
    colors: list[str] = card.get("color_identity") or []
    return "".join(colors)


def _escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _try_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def pascal_case(name: str) -> str:
    out: list[str] = []
    cap_next = True
    for ch in name:
        if ch.isalnum():
            out.append(ch.upper() if cap_next else ch)
            cap_next = False
        elif ch in ("'", "’"):
            # Drop apostrophes without breaking the word: Sazacap's -> Sazacaps.
            pass
        else:
            cap_next = True
    return "".join(out)
