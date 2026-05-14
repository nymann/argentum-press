"""Discover which cards argentum-engine already has implemented.

Ported from /Users/knj/code/github.com/nymann/argentum-engine/scripts/
missing-cards-report.py — same regexes, same DFC handling, same directory
convention. Kept as a small module here so the pipeline doesn't have to
shell out and parse the script's human-oriented output.
"""

from __future__ import annotations

import re
from pathlib import Path

# Matches `card("Name") { ... }` and `basicLand("Name") { ... }` DSL calls
# at the top of each card file.
_CARD_DSL_RE = re.compile(r'\b(?:card|basicLand)\(\s*"([^"]+)"')

# Matches `name = "..."` inside Printing(...) rows in reprint definitions.
_PRINTING_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')

# Matches `object FooSet : MtgSet` so we can recover the engine's chosen
# identifier prefix ("Foo") for naming the basic-lands file/vals.
_SET_OBJECT_RE = re.compile(r'\bobject\s+(\w+)Set\s*:\s*MtgSet\b')

_DEFINITIONS_REL = Path("mtg-sets/src/main/kotlin/com/wingedsheep/mtg/sets/definitions")


def set_root_dir(project_dir: Path, set_code: str) -> Path:
    """Where argentum-engine expects the set's top-level `*Set.kt` to live."""
    return project_dir / _DEFINITIONS_REL / set_code


def cards_dir(project_dir: Path, set_code: str) -> Path:
    """Where argentum-engine expects cards for `set_code` to live."""
    return set_root_dir(project_dir, set_code) / "cards"


def set_object_prefix(project_dir: Path, set_code: str) -> str | None:
    """If `<set>/*Set.kt` declares `object FooSet : MtgSet`, return 'Foo'.

    Used to derive the identifier prefix for the basic-lands file (e.g. BLB ->
    'Bloomburrow' -> 'BloomburrowBasicLands.kt'). Returns None for brand-new
    sets that haven't been scaffolded yet — callers fall back to the Scryfall
    set name."""
    root = set_root_dir(project_dir, set_code)
    if not root.is_dir():
        return None
    for kt_file in root.glob("*Set.kt"):
        text = kt_file.read_text(encoding="utf-8")
        match = _SET_OBJECT_RE.search(text)
        if match:
            return match.group(1)
    return None


def basic_lands_file(project_dir: Path, set_code: str, set_prefix: str) -> Path:
    """The expected path for a set's combined `<Prefix>BasicLands.kt`."""
    return cards_dir(project_dir, set_code) / f"{set_prefix}BasicLands.kt"


def implemented_cards_in_set(project_dir: Path, set_code: str) -> set[str]:
    """Display-names of cards already implemented under <project>/mtg-sets/.../
    <set_code>/cards. Returns an empty set if the directory doesn't exist
    yet — that's the "scaffold a new set from scratch" case.
    """
    target = cards_dir(project_dir, set_code)
    if not target.is_dir():
        return set()
    names: set[str] = set()
    for kt_file in target.glob("*.kt"):
        text = kt_file.read_text(encoding="utf-8")
        names.update(_CARD_DSL_RE.findall(text))
        names.update(_PRINTING_NAME_RE.findall(text))
    return {front_face(name) for name in names}


def front_face(name: str) -> str:
    """Strip ` // back` suffix from DFC / adventure names so we can match
    Scryfall's full names against argentum's front-face-only conventions."""
    return name.split(" // ", 1)[0].strip()
