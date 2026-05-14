# pyright: basic
"""Top-level card containers: ``Card``, ``TextBox``, ``TypeLine``, ``FlavorText``."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argentum_press.parser.ast.abilities import Ability
    from argentum_press.parser.ast.expressions import (
        ManaExpression,
        PTExpression,
        TypeExpression,
    )
    from argentum_press.parser.ast.references import Name


@dataclass(frozen=True, slots=True)
class FlavorText:
    """The italicised flavor text below the textbox."""

    text: str


@dataclass(frozen=True, slots=True)
class TextBox:
    """The rules-text box: a sequence of ability lines."""

    lines: tuple[Ability, ...] = ()


@dataclass(frozen=True, slots=True)
class TypeLine:
    """The middle line of a card: supertypes, types, subtypes."""

    supertypes: TypeExpression | None = None
    types: TypeExpression | None = None
    subtypes: TypeExpression | None = None


@dataclass(frozen=True, slots=True)
class Card:
    """A complete Magic card.

    Reed's MgCard accepted positional/keyword combinations with many optional
    parts; here every field is an explicit keyword-only optional. ``abilities``
    is provided as a convenience for callers that don't care about a full
    textbox structure.
    """

    name: Name | None = None
    mana_cost: ManaExpression | None = None
    color_indicator: object | None = None
    type_line: TypeLine | None = None
    loyalty: object | None = None
    expansion_symbol: object | None = None
    text_box: TextBox | None = None
    power_toughness: PTExpression | None = None
    hand_modifier: object | None = None
    life_modifier: object | None = None
    flavor: FlavorText | None = None
    abilities: tuple[Ability, ...] = field(default_factory=tuple)
