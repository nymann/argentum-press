# pyright: basic
"""Expression dataclasses for the parser AST.

Expressions describe values (numbers, power/toughness), declarations (the
nouns that effects act on), and effects (the verbs of card text).

The base classes (``Expression``, ``ValueExpression``, ``DeclarationExpression``,
``EffectExpression``, ``BinaryOp``, ``UnaryOp``, ``ValueComparisonExpression``,
``ManaSpecifier``) are plain Python classes used only for ``isinstance``/
``match`` dispatch — never instantiated directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argentum_press.parser.ast.references import DamageType


# ---------------------------------------------------------------------------
# Base hierarchies (plain classes, not dataclasses)
# ---------------------------------------------------------------------------


class Expression:
    """Base for the closed expression hierarchy."""


class ValueExpression(Expression):
    """Base for values: numbers, comparisons, "the number of …" decorations."""


class DeclarationExpression(Expression):
    """Anonymous declaration of a game entity (``target enchantment``)."""


class EffectExpression(Expression):
    """Base for verbs: ``destroy``, ``draw``, ``deal damage``, etc."""


class BinaryOp(Expression):
    """Logical/connective binary operators (``and``, ``or``, ``and/or``)."""


class UnaryOp(Expression):
    """Unary decorations (``target X``, ``non-Y``, ``each Z``, ``with W``)."""


class ValueComparisonExpression(ValueExpression):
    """Comparator phrase (``greater than``, ``equal to``, ``less than or equal``)."""


class ManaSpecifier(Expression):
    """Mana specifier (e.g. ``mana of any color``)."""


# ---------------------------------------------------------------------------
# Value-comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueGtExpression(ValueComparisonExpression):
    lhs: Expression | None
    rhs: Expression


@dataclass(frozen=True, slots=True)
class ValueGtEqExpression(ValueComparisonExpression):
    lhs: Expression | None
    rhs: Expression
    short_variant: bool = False


@dataclass(frozen=True, slots=True)
class ValueLtExpression(ValueComparisonExpression):
    lhs: Expression | None
    rhs: Expression


@dataclass(frozen=True, slots=True)
class ValueLtEqExpression(ValueComparisonExpression):
    lhs: Expression | None
    rhs: Expression
    short_variant: bool = False


@dataclass(frozen=True, slots=True)
class ValueEqExpression(ValueComparisonExpression):
    lhs: Expression | None
    rhs: Expression


# ---------------------------------------------------------------------------
# Numeric values
# ---------------------------------------------------------------------------


class NumberTypeEnum(Enum):
    LITERAL = auto()
    CARDINAL = auto()
    FREQUENCY = auto()
    ORDINAL = auto()
    CUSTOM = auto()


@dataclass(frozen=True, slots=True)
class NumberValue(ValueExpression):
    """A number value with an unparse-affecting type tag.

    ``value`` is an int for the numeric variants and a string for CUSTOM (e.g.
    ``"X"``, ``"*"``, ``"1+*"``).
    """

    value: int | str
    ntype: NumberTypeEnum = NumberTypeEnum.LITERAL


@dataclass(frozen=True, slots=True)
class NumberOfExpression(ValueExpression):
    """``the number of <expression>`` decoration."""

    expression: Expression


# ---------------------------------------------------------------------------
# Mana
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManaExpression(Expression):
    """A sequence of mana symbols (``{2}{W}{U}``)."""

    symbols: tuple[Expression, ...] = ()


@dataclass(frozen=True, slots=True)
class ManaSpecificationExpression(Expression):
    """Description of mana produced by an add-mana effect with a quantity
    and one or more specifiers, e.g. ``three mana of any one color``.
    """

    quantity: Expression
    specifiers: tuple[Expression, ...] = ()


@dataclass(frozen=True, slots=True)
class AnyColorSpecifier(ManaSpecifier):
    """``mana of any color`` (or ``of any one color`` if the flag is set)."""

    any_one_color: bool = False


# ---------------------------------------------------------------------------
# Cost expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostSequenceExpression(Expression):
    """Comma-separated cost sequence used in activated abilities."""

    arguments: tuple[Expression, ...] = ()


@dataclass(frozen=True, slots=True)
class DashCostExpression(Expression):
    """``— <cost>`` decoration (e.g. ``Cumulative upkeep — Add {R}``)."""

    cost: Expression


# ---------------------------------------------------------------------------
# Declarations / descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenericDeclarationExpression(DeclarationExpression):
    """Anonymous declaration that wraps a description tree."""

    definition: Expression


@dataclass(frozen=True, slots=True)
class DescriptionExpression(Expression):
    """A series of descriptor terms that describe an object."""

    descriptors: tuple[Expression, ...] = ()


# ---------------------------------------------------------------------------
# Power / toughness
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PTExpression(Expression):
    """Power/toughness expression (``5/5``, ``*/*``, ``X/X``)."""

    power: Expression
    toughness: Expression


# ---------------------------------------------------------------------------
# Color expression
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColorExpression(Expression):
    """A color expression (a single term or a connective tree)."""

    value: Expression


# ---------------------------------------------------------------------------
# Type expression
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TypeExpression(Expression):
    """A type-line component or in-text type fragment ("Snow Artifact")."""

    types: tuple[Expression, ...] = ()
    comma_delimited: bool = False


# ---------------------------------------------------------------------------
# Control expression
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlExpression(Expression):
    """``<controller> control(s)`` decoration."""

    controller: Expression


# ---------------------------------------------------------------------------
# Possessive expression
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PossessiveExpression(Expression):
    """Possession (``your graveyard``, ``its owner's hand``)."""

    possessor: Expression
    owned: Expression


# ---------------------------------------------------------------------------
# Modal expression
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModalExpression(Expression):
    """A series of modal choices (Abzan Charm)."""

    number_of_choices: Expression
    options: tuple[Expression, ...] = ()


# ---------------------------------------------------------------------------
# Change zone (the etb/ltb expression in card-text grammar)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangeZoneExpression(Expression):
    """``<subject> enters/leaves <zone>``."""

    subject: Expression
    zone: Expression
    entering: bool = True


# ---------------------------------------------------------------------------
# Binary operators
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AndExpression(BinaryOp):
    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, slots=True)
class OrExpression(BinaryOp):
    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, slots=True)
class AndOrExpression(BinaryOp):
    lhs: Expression
    rhs: Expression


# ---------------------------------------------------------------------------
# Unary operators
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetExpression(UnaryOp):
    """``target <X>`` or ``any target`` when ``is_any`` is True."""

    operand: Expression | None = None
    is_any: bool = False


@dataclass(frozen=True, slots=True)
class AllExpression(UnaryOp):
    operand: Expression


@dataclass(frozen=True, slots=True)
class EachExpression(UnaryOp):
    operand: Expression


@dataclass(frozen=True, slots=True)
class IndefiniteSingularExpression(UnaryOp):
    """``a`` / ``an`` decorator."""

    operand: Expression


@dataclass(frozen=True, slots=True)
class ChoiceExpression(UnaryOp):
    """``choose <X>``."""

    operand: Expression


@dataclass(frozen=True, slots=True)
class NonExpression(UnaryOp):
    """``non-<X>``."""

    operand: Expression


@dataclass(frozen=True, slots=True)
class WithExpression(UnaryOp):
    """``with <X>`` clause."""

    operand: Expression


@dataclass(frozen=True, slots=True)
class NamedExpression(UnaryOp):
    """``named <X>`` clause."""

    operand: Expression


# ---------------------------------------------------------------------------
# Effect expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AddManaExpression(EffectExpression):
    """``add <mana>`` (or ``<player> adds <mana>``)."""

    mana: Expression
    player: Expression | None = None


class DealsDamageVariant(Enum):
    """Surface ordering used by the unparser."""

    A = auto()  # <origin> deals <expr>? damage to <subject>?
    B = auto()  # <origin> deals damage <expr> to <subject>
    C = auto()  # <origin> deals damage to <subject> <expr>


@dataclass(frozen=True, slots=True)
class DealsDamageExpression(EffectExpression):
    """``<origin> deals <amount?> <damage_type> to <subject?>``."""

    origin: Expression
    damage_type: DamageType
    damage_amount: Expression | None = None
    subject: Expression | None = None
    variant: DealsDamageVariant = DealsDamageVariant.A


@dataclass(frozen=True, slots=True)
class DestroyExpression(EffectExpression):
    """``destroy <subject>``."""

    subject: Expression


@dataclass(frozen=True, slots=True)
class SacrificeExpression(EffectExpression):
    """``sacrifice <subject>`` (optionally with a controlling player)."""

    subject: Expression
    controller: Expression | None = None


@dataclass(frozen=True, slots=True)
class ExileExpression(EffectExpression):
    """``exile <subject>``."""

    subject: Expression


@dataclass(frozen=True, slots=True)
class TapUntapExpression(EffectExpression):
    """Tap, untap, or "tap or untap" depending on the two flags."""

    subject: Expression
    tap: bool = True
    untap: bool = False


@dataclass(frozen=True, slots=True)
class ReturnExpression(EffectExpression):
    """``return <subject> [from <origin>] to <destination>``."""

    subject: Expression
    destination: Expression
    origin: Expression | None = None


@dataclass(frozen=True, slots=True)
class UncastExpression(EffectExpression):
    """Counterspell effect (kept internally as "uncast")."""

    subject: Expression


@dataclass(frozen=True, slots=True)
class GainLoseExpression(EffectExpression):
    """``gain/lose <subject>`` — Reed left this stubbed; carried forward."""

    subject: Expression | None = None


@dataclass(frozen=True, slots=True)
class AddRemoveExpression(EffectExpression):
    """``add/remove`` (e.g. counters). Reed left this stubbed."""

    subject: Expression | None = None


@dataclass(frozen=True, slots=True)
class CreateTokenExpression(EffectExpression):
    """``create <quantity?> <descriptor>``."""

    descriptor: Expression
    quantity: Expression | None = None


@dataclass(frozen=True, slots=True)
class CardDrawExpression(EffectExpression):
    """``draw <quantity> cards``."""

    quantity: Expression


@dataclass(frozen=True, slots=True)
class SearchLibraryExpression(EffectExpression):
    """``search <owner> library for <subject>``."""

    owner: Expression
    subject: Expression


@dataclass(frozen=True, slots=True)
class ShuffleLibraryExpression(EffectExpression):
    """``shuffle <owner> library``."""

    owner: Expression


@dataclass(frozen=True, slots=True)
class PutInZoneExpression(EffectExpression):
    """``put <subject> into <zone> [<conditions>]``."""

    subject: Expression
    zone: Expression
    conditions: Expression | None = None


@dataclass(frozen=True, slots=True)
class RevealExpression(EffectExpression):
    """Reveal effect placeholder; Reed left this stubbed."""
