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

_DEFINITIONS_REL = Path("mtg-sets/src/main/kotlin/com/wingedsheep/mtg/sets/definitions")


def cards_dir(project_dir: Path, set_code: str) -> Path:
    """Where argentum-engine expects cards for `set_code` to live."""
    return project_dir / _DEFINITIONS_REL / set_code / "cards"


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
