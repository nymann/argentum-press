# pyright: basic
"""References and modifier nodes.

References point back to previously defined game entities (names, ``self``,
``this``, ``that``, ``it``). Modifier dataclasses carry the descriptive
shorthand used elsewhere in card text (zones, qualifiers, characteristics,
time terms, tap/untap, declaration modifiers).

The base classes (``Reference``, ``DeclarationModifier``, ``TimeTerm``) are
plain Python classes used only for ``isinstance``/``match`` dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argentum_press.parser.ast.expressions import Expression


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


class Reference:
    """Base for the closed reference hierarchy."""


@dataclass(frozen=True, slots=True)
class Name:
    """A bare name string, e.g. a card name on the type line."""

    name: str = ""


@dataclass(frozen=True, slots=True)
class NameReference(Reference):
    """A stand-in (``~``/``~f``) for a card's own name in its text.

    ``antecedent`` resolves to the originating :class:`Name` once binding has
    occurred; it is ``None`` until then. ``first_name_only`` distinguishes the
    ``~f`` form for legendary cards (e.g. "discard Arashi").
    """

    antecedent: Name | None = None
    first_name_only: bool = False


class SelfRefEnum(Enum):
    NEUTRAL = "itself"
    MALE = "himself"
    FEMALE = "herself"


@dataclass(frozen=True, slots=True)
class SelfReference(Reference):
    """``itself``, ``himself``, or ``herself`` reference."""

    reftype: SelfRefEnum = SelfRefEnum.NEUTRAL
    antecedent: object | None = None


@dataclass(frozen=True, slots=True)
class ThatReference(Reference):
    """``that <descriptor>`` reference to a previously defined entity."""

    descriptor: Expression
    antecedent: object | None = None


@dataclass(frozen=True, slots=True)
class ItReference(Reference):
    """``it`` reference; no descriptor, optional resolved antecedent."""

    antecedent: object | None = None


@dataclass(frozen=True, slots=True)
class ThisReference(Reference):
    """``this <descriptor>`` reference, typically used in granted abilities."""

    descriptor: Expression
    antecedent: object | None = None


@dataclass(frozen=True, slots=True)
class AbilityReference(Reference):
    """Generic reference to an ability (e.g. Cairn Wanderer's protection)."""

    antecedent: object | None = None


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


class PlayerTermEnum(Enum):
    YOU = "you"
    OPPONENT = "opponent"
    TEAMMATE = "teammate"
    TEAM = "team"
    OWNER = "owner"
    CONTROLLER = "controller"
    PLAYER = "player"


# Map for unparsing variants. Keeps Reed's nominative_plural/possessive forms.
PLAYER_TERM_FORMS: dict[PlayerTermEnum, dict[str, str]] = {
    PlayerTermEnum.YOU: {
        "nominative_singular": "you",
        "nominative_plural": "you",
        "possessive_singular": "your",
        "possessive_plural": "your",
    },
    PlayerTermEnum.OPPONENT: {
        "nominative_singular": "opponent",
        "nominative_plural": "opponents",
        "possessive_singular": "opponent's",
        "possessive_plural": "opponents'",
    },
    PlayerTermEnum.TEAMMATE: {
        "nominative_singular": "teammate",
        "nominative_plural": "teammates",
        "possessive_singular": "teammate's",
        "possessive_plural": "teammates'",
    },
    PlayerTermEnum.TEAM: {
        "nominative_singular": "team",
        "nominative_plural": "teams",
        "possessive_singular": "team's",
        "possessive_plural": "teams'",
    },
    PlayerTermEnum.OWNER: {
        "nominative_singular": "owner",
        "nominative_plural": "owners",
        "possessive_singular": "owner's",
        "possessive_plural": "owners'",
    },
    PlayerTermEnum.CONTROLLER: {
        "nominative_singular": "controller",
        "nominative_plural": "controllers",
        "possessive_singular": "controller's",
        "possessive_plural": "controllers'",
    },
    PlayerTermEnum.PLAYER: {
        "nominative_singular": "player",
        "nominative_plural": "players",
        "possessive_singular": "player's",
        "possessive_plural": "players'",
    },
}


@dataclass(frozen=True, slots=True)
class PlayerTerm:
    """A description of a player (``you``, ``opponent``, ``each player``)."""

    value: PlayerTermEnum
    is_plural: bool = False


# ---------------------------------------------------------------------------
# Damage type
# ---------------------------------------------------------------------------


class DamageTypeEnum(Enum):
    REGULAR = "damage"
    NONCOMBAT = "noncombat damage"
    COMBAT = "combat damage"


@dataclass(frozen=True, slots=True)
class DamageType:
    """Damage type marker used by deals-damage expressions."""

    value: DamageTypeEnum = DamageTypeEnum.REGULAR


# ---------------------------------------------------------------------------
# Declaration modifiers
# ---------------------------------------------------------------------------


class DeclarationModifier:
    """Base for the closed declaration-modifier hierarchy."""


class AbilityModifierEnum(Enum):
    TRIGGERED = "triggered"
    ACTIVATED = "activated"
    MANA = "mana"
    LOYALTY = "loyalty"


@dataclass(frozen=True, slots=True)
class AbilityModifier(DeclarationModifier):
    """E.g. ``triggered`` in ``triggered ability``."""

    value: AbilityModifierEnum


class CombatStatusEnum(Enum):
    ATTACKING = "attacking"
    DEFENDING = "defending"
    ATTACKED = "attacked"
    BLOCKING = "blocking"
    BLOCKED = "blocked"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class CombatStatusModifier(DeclarationModifier):
    """E.g. ``attacking`` in ``attacking creature``."""

    value: CombatStatusEnum


class KeywordStatusEnum(Enum):
    PAIRED = "paired"
    KICKED = "kicked"
    FACE_UP = "face-up"
    FACE_DOWN = "face-down"
    TRANSFORMED = "transformed"
    ENCHANTED = "enchanted"
    EQUIPPED = "equipped"
    FORTIFIED = "fortified"
    MONSTROUS = "monstrous"
    SUSPENDED = "suspended"


@dataclass(frozen=True, slots=True)
class KeywordStatusModifier(DeclarationModifier):
    """E.g. ``kicked`` in ``if the kicker cost was paid``."""

    value: KeywordStatusEnum


class TapStatusEnum(Enum):
    TAPPED = "tapped"
    UNTAPPED = "untapped"


@dataclass(frozen=True, slots=True)
class TapStatusModifier(DeclarationModifier):
    """``tapped`` / ``untapped``."""

    value: TapStatusEnum


class EffectStatusEnum(Enum):
    NAMED = "named"
    CHOSEN = "chosen"
    REVEALED = "revealed"
    RETURNED = "returned"
    DESTROYED = "destroyed"
    EXILED = "exiled"
    DIED = "died"
    COUNTERED = "countered"
    SACRIFICED = "sacrificed"
    TARGETED = "the target of a spell or ability"
    PREVENTED = "prevented"


@dataclass(frozen=True, slots=True)
class EffectStatusModifier(DeclarationModifier):
    """E.g. ``destroyed`` / ``exiled``."""

    value: EffectStatusEnum


# ---------------------------------------------------------------------------
# Characteristics
# ---------------------------------------------------------------------------


class CharacteristicEnum(Enum):
    NAME = "name"
    MANA_COST = "mana cost"
    COLOR_INDICATOR = "color indicator"
    CARD_TYPE = "card type"
    SUBTYPE = "subtype"
    SUPERTYPE = "supertype"
    RULES_TEXT = "rules text"
    ABILITIES = "abilities"
    POWER = "power"
    TOUGHNESS = "toughness"
    LOYALTY = "loyalty"
    HAND_MODIFIER = "hand modifier"
    LIFE_MODIFIER = "life modifier"


@dataclass(frozen=True, slots=True)
class CharacteristicTerm:
    """The characteristic of a previously defined object."""

    value: CharacteristicEnum


# ---------------------------------------------------------------------------
# Qualifier
# ---------------------------------------------------------------------------


class QualifierEnum(Enum):
    ABILITY = "ability"
    CARD = "card"
    PERMANENT = "permanent"
    SOURCE = "source"
    SPELL = "spell"
    TOKEN = "token"


@dataclass(frozen=True, slots=True)
class Qualifier:
    """Object-state qualifier (``Elf spell``, ``Elf token``)."""

    value: QualifierEnum


# ---------------------------------------------------------------------------
# Time terms
# ---------------------------------------------------------------------------


class TimeTerm:
    """Base for time terms (phase, step, turn)."""


class PhaseEnum(Enum):
    BEGINNING = "beginning phase"
    PRECOMBAT_MAIN = "precombat main phase"
    COMBAT = "combat phase"
    POSTCOMBAT_MAIN = "postcombat main phase"
    ENDING = "ending phase"


@dataclass(frozen=True, slots=True)
class PhaseTerm(TimeTerm):
    """A phase such as the precombat main phase."""

    value: PhaseEnum


class StepEnum(Enum):
    UNTAP = "untap step"
    UPKEEP = "upkeep step"
    DRAW = "draw step"
    BEGINNING_OF_COMBAT = "beginning of combat"
    DECLARE_ATTACKERS = "declare attackers step"
    DECLARE_BLOCKERS = "declare blockers step"
    COMBAT_DAMAGE = "combat damage step"
    END_OF_COMBAT = "end of combat"
    END_STEP = "end step"
    CLEANUP_STEP = "cleanup step"


@dataclass(frozen=True, slots=True)
class StepTerm(TimeTerm):
    """A step such as the draw step."""

    value: StepEnum


@dataclass(frozen=True, slots=True)
class TurnTerm(TimeTerm):
    """A turn ("your turn", "next turn", etc.)."""


# ---------------------------------------------------------------------------
# Zone
# ---------------------------------------------------------------------------


class ZoneEnum(Enum):
    BATTLEFIELD = "the battlefield"
    GRAVEYARD = "graveyard"
    LIBRARY = "library"
    HAND = "hand"
    STACK = "stack"
    EXILE = "exile"
    COMMAND = "command zone"
    OUTSIDE = "outside the game"
    ANYWHERE = "anywhere"


@dataclass(frozen=True, slots=True)
class Zone:
    """A zone reference (``the battlefield``, ``graveyard``, etc.)."""

    value: ZoneEnum


# ---------------------------------------------------------------------------
# Tap/untap symbol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TapUntapSymbol:
    """The {T} or {Q} cost symbol."""

    is_tap: bool = True
