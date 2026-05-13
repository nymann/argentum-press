"""Concrete spec of the AST surface argentum-press expects from mtgcompiler.

While mtgcompiler is being adapted, this module IS the contract: classes,
attributes, semantics. Once mtgcompiler exposes the matching surface this
file collapses to:

    from mtgcompiler.ast import *  # noqa: F401, F403

The Lowerer (lowerer.py) dispatches via functools.singledispatchmethod over
these types; replacing them with re-exports leaves the Lowerer code intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ----- keywords / triggers / targets -----

class Keyword(Enum):
    FLYING = "flying"
    TRAMPLE = "trample"
    FIRST_STRIKE = "first strike"
    DOUBLE_STRIKE = "double strike"
    DEATHTOUCH = "deathtouch"
    LIFELINK = "lifelink"
    HASTE = "haste"
    VIGILANCE = "vigilance"
    REACH = "reach"
    MENACE = "menace"
    HEXPROOF = "hexproof"
    SHROUD = "shroud"
    INDESTRUCTIBLE = "indestructible"
    FLASH = "flash"
    DEFENDER = "defender"
    INTIMIDATE = "intimidate"
    FEAR = "fear"
    SHADOW = "shadow"
    HORSEMANSHIP = "horsemanship"
    PROWESS = "prowess"
    CHANGELING = "changeling"


class TriggerCondition(Enum):
    ENTERS_BATTLEFIELD = "enters"
    ATTACKS = "attacks"
    DIES = "dies"
    BEGINNING_OF_UPKEEP = "upkeep"
    END_OF_TURN = "end_of_turn"


# Targets are a sealed-ish hierarchy. Use frozen dataclasses so isinstance
# / match work uniformly.

class Target:
    pass


@dataclass(frozen=True, slots=True)
class AnyTarget(Target):
    pass


@dataclass(frozen=True, slots=True)
class TargetCreature(Target):
    pass


@dataclass(frozen=True, slots=True)
class TargetPlayer(Target):
    pass


@dataclass(frozen=True, slots=True)
class TargetSelf(Target):
    pass


# ----- effects -----

class Effect:
    pass


@dataclass(frozen=True, slots=True)
class DrawCards(Effect):
    amount: int


@dataclass(frozen=True, slots=True)
class GainLife(Effect):
    amount: int


@dataclass(frozen=True, slots=True)
class LoseLife(Effect):
    amount: int


@dataclass(frozen=True, slots=True)
class DealDamage(Effect):
    amount: int
    target: Target


@dataclass(frozen=True, slots=True)
class DestroyTarget(Effect):
    target: Target


@dataclass(frozen=True, slots=True)
class CreateToken(Effect):
    token_name: str
    count: int


@dataclass(frozen=True, slots=True)
class ModifyStats(Effect):
    power_delta: int
    toughness_delta: int
    target: Target


@dataclass(frozen=True, slots=True)
class ReanimateTarget(Effect):
    target: Target


@dataclass(frozen=True, slots=True)
class ShuffleSelfIntoLibrary(Effect):
    pass


# ----- costs -----

class Cost:
    pass


@dataclass(frozen=True, slots=True)
class ManaCost(Cost):
    symbols: str


@dataclass(frozen=True, slots=True)
class TapSelf(Cost):
    pass


@dataclass(frozen=True, slots=True)
class SacrificeSelf(Cost):
    pass


# ----- abilities -----

class Ability:
    pass


@dataclass(frozen=True, slots=True)
class KeywordAbility(Ability):
    keyword: Keyword


@dataclass(frozen=True, slots=True)
class SpellAbility(Ability):
    effects: tuple[Effect, ...]


@dataclass(frozen=True, slots=True)
class TriggeredAbility(Ability):
    condition: TriggerCondition
    effects: tuple[Effect, ...]


@dataclass(frozen=True, slots=True)
class ActivatedAbility(Ability):
    costs: tuple[Cost, ...]
    effects: tuple[Effect, ...]


# ----- card -----

@dataclass(frozen=True, slots=True)
class Card:
    abilities: tuple[Ability, ...] = field(default_factory=tuple)


# ----- parse-result -----

@dataclass(frozen=True, slots=True)
class ParseError:
    kind: str  # one of: "incomplete", "invalid", "ambiguous"
    message: str
    position: int | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    ast: Card | None = None
    error: ParseError | None = None

    @property
    def ok(self) -> bool:
        return self.ast is not None
