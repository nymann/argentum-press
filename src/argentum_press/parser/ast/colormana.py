# pyright: basic
r"""Mana symbols and color terms.

A ``ManaSymbol`` describes a single mana symbol such as ``{G\P}`` or ``{U/W}``
(the ``\P`` here is intentional, inside a raw docstring so Python does not warn
about an invalid escape). A ``ColorTerm`` describes the textual color terms
like ``green`` or ``multicolored`` that appear in card text.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, Flag, auto


class ManaType(Flag):
    """The six base mana types. Hybrid symbols combine these via OR."""

    WHITE = auto()
    BLUE = auto()
    BLACK = auto()
    RED = auto()
    GREEN = auto()
    COLORLESS = auto()


class ManaModifier(Flag):
    """Modifiers that may be combined with a mana type."""

    PHYREXIAN = auto()
    SNOW = auto()
    ALTERNATE_TWO = auto()
    HALF = auto()


@dataclass(frozen=True, slots=True)
class ManaSymbol:
    """A single mana symbol such as ``{G}``, ``{U/W}``, ``{2/R}``, ``{X}``.

    ``color`` is None for generic mana (and the cost-value goes in ``cvalue``).
    ``modifiers`` is None unless the symbol carries Phyrexian, snow, etc.
    ``cvalue`` is only meaningful for generic costs; it may be an int or a
    custom string such as "X" or "*".
    """

    color: ManaType | None = None
    modifiers: ManaModifier | None = None
    cvalue: int | str = 0


class ColorTermEnum(Enum):
    """Color terms that appear verbatim in rules text."""

    WHITE = "white"
    BLUE = "blue"
    BLACK = "black"
    RED = "red"
    GREEN = "green"
    MONOCOLORED = "monocolored"
    MULTICOLORED = "multicolored"
    COLORLESS = "colorless"


@dataclass(frozen=True, slots=True)
class ColorTerm:
    """A color term such as ``green`` or ``multicolored``.

    ``value`` is a :class:`ColorTermEnum` ordinarily, or a free-form string
    when a card uses a custom term not modelled by the enum.
    """

    value: ColorTermEnum | str
