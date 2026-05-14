# pyright: basic
"""Ability dataclasses for the parser AST.

Reed Milewicz's MgKeywordAbility had ~100 subclasses. The pure-marker ones
(flying, trample, deathtouch, ...) collapse to a single
:class:`SimpleKeywordAbility` tagged with a :class:`Keyword` enum. The
parametric ones (equip, ward, cycling, kicker, ...) remain as distinct
dataclasses since each carries typed fields.

The base classes (``Ability``, ``KeywordAbility``) are plain Python classes
used only for ``isinstance``/``match`` dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from argentum_press.parser.ast.keywords import Keyword

if TYPE_CHECKING:
    from argentum_press.parser.ast.expressions import Expression
    from argentum_press.parser.ast.references import Name
    from argentum_press.parser.ast.statements import Statement, StatementBlock


# ---------------------------------------------------------------------------
# Decoration nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReminderText:
    """Italicised reminder text in parentheses attached to an ability."""

    text: str


@dataclass(frozen=True, slots=True)
class AbilityWord:
    """An ability-word decoration such as ``Battalion —`` or ``Spell Mastery —``."""

    word: str


@dataclass(frozen=True, slots=True)
class StatementSequence:
    """Bare sequence of statements (Reed kept this separate from StatementBlock)."""

    statements: tuple[Statement, ...] = ()


# ---------------------------------------------------------------------------
# Ability bases
# ---------------------------------------------------------------------------


class Ability:
    """Base for the closed ability hierarchy."""


class KeywordAbility(Ability):
    """Base for keyword abilities. Concrete subclasses follow."""


# ---------------------------------------------------------------------------
# Non-keyword abilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegularAbility(Ability):
    """Generic non-keyword ability whose body is a statement block."""

    block: StatementBlock
    ability_word: AbilityWord | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class SpellAbility(Ability):
    """An instant/sorcery spell ability."""

    instructions: Statement
    ability_word: AbilityWord | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ActivatedAbility(Ability):
    """``<cost>: <instructions>`` ability."""

    cost: Expression
    instructions: Statement
    ability_word: AbilityWord | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class TriggeredAbility(Ability):
    """``<condition>, <outcome>`` ability (``when``/``whenever``/``at``)."""

    condition: Statement | Expression
    outcome: Statement
    ability_word: AbilityWord | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class StaticAbility(Ability):
    """Static abilities are written as statements; they are simply true."""

    block: StatementBlock | None = None
    ability_word: AbilityWord | None = None
    reminder_text: ReminderText | None = None


# ---------------------------------------------------------------------------
# Collapsed simple keyword abilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimpleKeywordAbility(KeywordAbility):
    """A keyword ability with no typed payload beyond an optional reminder.

    Use this for the ~60 pure marker keywords: flying, trample, deathtouch,
    lifelink, haste, vigilance, reach, menace, hexproof (plain), shroud,
    indestructible, flash, defender, first strike, double strike, intimidate,
    fear, shadow, horsemanship, prowess, changeling, phasing, conspire,
    persist, wither, retrace, exalted, cascade, rebound, totem armor,
    infect, battle cry, living weapon, undying, soulbond, unleash, cipher,
    evolve, extort, fuse, dethrone, exploit, devoid, ingest, myriad, skulk,
    melee, improvise, aftermath, ascend, assist, mentor, hideaway,
    gravestorm, split second, haunt, epic, delve, convoke, sunburst,
    storm, provoke, dredge..., etc.
    """

    keyword: Keyword
    reminder_text: ReminderText | None = None


# ---------------------------------------------------------------------------
# Parametric keyword abilities (preserved as distinct dataclasses)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquipAbility(KeywordAbility):
    """``Equip [quality] [cost]`` (the quality is optional)."""

    cost: Expression
    quality: Expression | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EnchantAbility(KeywordAbility):
    """``Enchant <descriptor>``."""

    descriptor: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class HexproofAbility(KeywordAbility):
    """``Hexproof`` or ``Hexproof from <quality>``.

    Plain hexproof can also be expressed as ``SimpleKeywordAbility(Keyword.HEXPROOF)``;
    use this class when a quality is present.
    """

    quality: Expression | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class LandwalkAbility(KeywordAbility):
    """``<landtype>walk`` or generic ``landwalk`` (Reed's MgLandwalkAbility)."""

    landtype: Expression | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ProtectionAbility(KeywordAbility):
    """``Protection from <quality>[ and from <quality>]*``."""

    qualities: tuple[Expression, ...] = ()
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class BandingAbility(KeywordAbility):
    """``Banding`` or ``Bands with other <quality>``."""

    quality: Expression | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class RampageAbility(KeywordAbility):
    """``Rampage N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class CumulativeUpkeepAbility(KeywordAbility):
    """``Cumulative upkeep <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class BuybackAbility(KeywordAbility):
    """``Buyback <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class CyclingAbility(KeywordAbility):
    """``Cycling <cost>`` or ``<type>cycling <cost>``."""

    cost: Expression
    cycling_type: Expression | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EchoAbility(KeywordAbility):
    """``Echo <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class FadingAbility(KeywordAbility):
    """``Fading N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class KickerAbility(KeywordAbility):
    """``Kicker <cost>`` or ``Multikicker <cost>`` (with the is_multi flag)."""

    cost: Expression
    is_multi: bool = False
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class FlashbackAbility(KeywordAbility):
    """``Flashback <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class MadnessAbility(KeywordAbility):
    """``Madness <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class MorphAbility(KeywordAbility):
    """``Morph <cost>`` or ``Megamorph <cost>``."""

    cost: Expression
    is_mega: bool = False
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class AmplifyAbility(KeywordAbility):
    """``Amplify N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class AffinityAbility(KeywordAbility):
    """``Affinity for <descriptor>``."""

    descriptor: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EntwineAbility(KeywordAbility):
    """``Entwine <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ModularAbility(KeywordAbility):
    """``Modular N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class BushidoAbility(KeywordAbility):
    """``Bushido N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class SoulshiftAbility(KeywordAbility):
    """``Soulshift N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class SpliceAbility(KeywordAbility):
    """``Splice onto <subtype> <cost>``."""

    cost: Expression
    splice_type: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class OfferingAbility(KeywordAbility):
    """``<subtype> offering``."""

    descriptor: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class NinjutsuAbility(KeywordAbility):
    """``Ninjutsu <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ForecastAbility(KeywordAbility):
    """``Forecast — <activated ability>``."""

    activated_ability: ActivatedAbility
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class DredgeAbility(KeywordAbility):
    """``Dredge N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class TransmuteAbility(KeywordAbility):
    """``Transmute <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class BloodthirstAbility(KeywordAbility):
    """``Bloodthirst N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ReplicateAbility(KeywordAbility):
    """``Replicate <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class GraftAbility(KeywordAbility):
    """``Graft N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class RecoverAbility(KeywordAbility):
    """``Recover <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class RippleAbility(KeywordAbility):
    """``Ripple N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class SuspendAbility(KeywordAbility):
    """``Suspend N — <cost>``."""

    caliber: Expression
    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class VanishingAbility(KeywordAbility):
    """``Vanishing N`` (or plain ``vanishing``)."""

    caliber: Expression | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class AbsorbAbility(KeywordAbility):
    """``Absorb N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class AuraSwapAbility(KeywordAbility):
    """``Aura swap <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class FortifyAbility(KeywordAbility):
    """``Fortify <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class FrenzyAbility(KeywordAbility):
    """``Frenzy N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class PoisonousAbility(KeywordAbility):
    """``Poisonous N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class TransfigureAbility(KeywordAbility):
    """``Transfigure <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ChampionAbility(KeywordAbility):
    """``Champion a <descriptor>``."""

    descriptor: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EvokeAbility(KeywordAbility):
    """``Evoke <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ProwlAbility(KeywordAbility):
    """``Prowl <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ReinforceAbility(KeywordAbility):
    """``Reinforce N — <cost>``."""

    caliber: Expression
    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class DevourAbility(KeywordAbility):
    """``Devour N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class UnearthAbility(KeywordAbility):
    """``Unearth <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class AnnihilatorAbility(KeywordAbility):
    """``Annihilator N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class LevelUpAbility(KeywordAbility):
    """``Level up <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class MiracleAbility(KeywordAbility):
    """``Miracle <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class OverloadAbility(KeywordAbility):
    """``Overload <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class ScavengeAbility(KeywordAbility):
    """``Scavenge <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class BestowAbility(KeywordAbility):
    """``Bestow <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class TributeAbility(KeywordAbility):
    """``Tribute N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class HiddenAgendaAbility(KeywordAbility):
    """``Hidden agenda`` or ``Double agenda``."""

    is_double_agenda: bool = False
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class OutlastAbility(KeywordAbility):
    """``Outlast <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class DashAbility(KeywordAbility):
    """``Dash <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class RenownAbility(KeywordAbility):
    """``Renown N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class AwakenAbility(KeywordAbility):
    """``Awaken N — <cost>``."""

    caliber: Expression
    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class SurgeAbility(KeywordAbility):
    """``Surge <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EmergeAbility(KeywordAbility):
    """``Emerge <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EscalateAbility(KeywordAbility):
    """``Escalate <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class CrewAbility(KeywordAbility):
    """``Crew N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class FabricateAbility(KeywordAbility):
    """``Fabricate N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class PartnerAbility(KeywordAbility):
    """``Partner`` or ``Partner with <name>``.

    Plain partner can also be ``SimpleKeywordAbility(Keyword.PARTNER)``;
    use this class for the ``partner with`` variant.
    """

    partner_name: Name | None = None
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EmbalmAbility(KeywordAbility):
    """``Embalm <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class EternalizeAbility(KeywordAbility):
    """``Eternalize <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class AfflictAbility(KeywordAbility):
    """``Afflict N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class SurveilAbility(KeywordAbility):
    """``Surveil N``."""

    caliber: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class JumpStartAbility(KeywordAbility):
    """``Jump-start <cost>``."""

    cost: Expression
    reminder_text: ReminderText | None = None


@dataclass(frozen=True, slots=True)
class WardAbility(KeywordAbility):
    """``Ward <cost>``.

    Reed did not actually have a dedicated MgWardAbility (ward postdates that
    codebase), but the requirement names this as parametric so we include it
    pre-emptively for the transformer.
    """

    cost: Expression
    reminder_text: ReminderText | None = None
