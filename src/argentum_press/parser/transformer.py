# pyright: basic
"""Lower a Lark parse tree to argentum-press's frozen-dataclass AST.

This is the bridge between :mod:`argentum_press.parser.grammar` (Reed's
939-line Lark grammar, lifted verbatim) and :mod:`argentum_press.parser.ast`
(the new flat dataclass surface that replaced Reed's ~250 ``Mg*`` IR
classes). The grammar is enormous; we deliberately cover only the shapes
exercised by the day-one BLB smoke. Anything we don't yet model surfaces
as :class:`LoweringIncomplete` so the caller can grep the failure label.

Strategy
--------
We subclass :class:`lark.Transformer`. Lark walks the tree bottom-up and
calls a method named after each rule with the (already-transformed) list
of children. This matches Reed's original ``MtgJsonTransformer`` and lets
us write one focused method per rule without any explicit dispatch.

Methods we don't define fall through to :meth:`Transformer.__default__`,
which we override to raise :class:`LoweringIncomplete`. That keeps the
contract honest: if a parsed shape reaches us we have either modelled it
or surfaced a labelled error.

Public API
----------
* :func:`transform` - lower a ``cardtext`` ``lark.Tree`` to a
  :class:`~argentum_press.parser.ast.Card`.
* :func:`parse` - the higher-level convenience: take a Scryfall dict or
  raw oracle text, run the preprocessor, parse, and lower, returning a
  :class:`ParseResult`.
* :class:`ParseResult`, :class:`ParseError` - return shape for
  :func:`parse`. Mirrors mtgcompiler's existing public shape so consumers
  can switch backends with minimal churn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import lark
from lark import Token, Transformer, Tree
from lark.exceptions import LarkError, UnexpectedInput

from argentum_press.parser.ast import (
    AbilitySequenceStatement,
    AbilityWord,
    AbsorbAbility,
    ActivationRestrictionStatement,
    ActivationStatement,
    AddRemoveExpression,
    AffinityAbility,
    AfflictAbility,
    AmplifyAbility,
    AndExpression,
    AndOrExpression,
    AnnihilatorAbility,
    AnyColorSpecifier,
    AsLongAsStatement,
    AsStatement,
    AtStatement,
    AuraSwapAbility,
    AwakenAbility,
    BandingAbility,
    BeingStatement,
    BestowAbility,
    BloodthirstAbility,
    BushidoAbility,
    BuybackAbility,
    Card,
    CardDrawExpression,
    CastExpression,
    ChampionAbility,
    ChangeZoneExpression,
    ChoiceExpression,
    ColorExpression,
    CompoundStatement,
    CompoundTerminator,
    ConniveExpression,
    ControlExpression,
    CopyExpression,
    CostIncreaseStatement,
    CostSequenceExpression,
    CreateTokenExpression,
    CrewAbility,
    CumulativeUpkeepAbility,
    CyclingAbility,
    DamageType,
    DamageTypeEnum,
    DashAbility,
    DashCostExpression,
    DealsDamageExpression,
    DealsDamageVariant,
    DescriptionExpression,
    DestroyExpression,
    DevourAbility,
    DredgeAbility,
    EachExpression,
    EchoAbility,
    EmbalmAbility,
    EmergeAbility,
    EnchantAbility,
    EntwineAbility,
    EquipAbility,
    EscalateAbility,
    EternalizeAbility,
    EvokeAbility,
    ExceptStatement,
    ExileExpression,
    Expression,
    ExpressionStatement,
    FabricateAbility,
    FadingAbility,
    FlashbackAbility,
    ForStatement,
    FortifyAbility,
    FrenzyAbility,
    GainLoseExpression,
    GenericDeclarationExpression,
    GraftAbility,
    HexproofAbility,
    HiddenAgendaAbility,
    IfStatement,
    IndefiniteSingularExpression,
    ItReference,
    JumpStartAbility,
    Keyword,
    KeywordAbility,
    KeywordAbilityListStatement,
    KickerAbility,
    LandwalkAbility,
    LevelUpAbility,
    LookExpression,
    MadnessAbility,
    ManaExpression,
    MayStatement,
    MayhemAbility,
    MiracleAbility,
    ModalChoice,
    ModalExpression,
    ModularAbility,
    MorphAbility,
    Name,
    NameReference,
    NamedExpression,
    NinjutsuAbility,
    NonExpression,
    NumberTypeEnum,
    NumberValue,
    OfferingAbility,
    OrExpression,
    OutlastAbility,
    OverloadAbility,
    PartnerAbility,
    PoisonousAbility,
    PreventDamageExpression,
    ProtectionAbility,
    ProwlAbility,
    PTExpression,
    RampageAbility,
    RecoverAbility,
    RedirectAllDamageExpression,
    RegularAbility,
    ReinforceAbility,
    ReminderText,
    RenownAbility,
    ReplicateAbility,
    ReturnExpression,
    RevealExpression,
    RippleAbility,
    SacrificeExpression,
    ScavengeAbility,
    SearchLibraryExpression,
    SelfReference,
    ShuffleLibraryExpression,
    SimpleKeywordAbility,
    SoulshiftAbility,
    SpliceAbility,
    Statement,
    StatementBlock,
    SuspendAbility,
    SurgeAbility,
    SurveilAbility,
    SurveilExpression,
    TapUntapExpression,
    TargetExpression,
    TextBox,
    ThereExistsStatement,
    TransfigureAbility,
    TransmuteAbility,
    TributeAbility,
    TriggerRestrictionStatement,
    TriggeredAbility,
    TypeExpression,
    UncastExpression,
    UnearthAbility,
    UntilStatement,
    ValueGtEqExpression,
    ValueLtEqExpression,
    VanishingAbility,
    WardAbility,
    WebSlingingAbility,
    WhenStatement,
    WheneverStatement,
    WithExpression,
)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class LoweringIncomplete(Exception):
    """Raised when the transformer hits a grammar rule it doesn't model yet.

    Message is a short, machine-grep-able label (e.g.
    ``"unmodeled-rule:fightexpression"``) so it can flow straight into
    :attr:`ParseError.message`.
    """


@dataclass(frozen=True, slots=True)
class ParseErrorDetails:
    """Structured fields extracted from a Lark :class:`UnexpectedInput`.

    Lark's ``str(exc)`` is rich (line/col, expected-tokens list, context
    marker) but multi-line. :attr:`ParseError.message` keeps only the first
    line so it stays grep-friendly as a label, which throws away the most
    actionable signal for grammar fixes. This struct recovers it so the
    fix-loop orchestrator can surface the full picture without re-running
    the parser.
    """

    preprocessed_text: str
    """The exact text Lark saw - i.e. after :func:`_preprocess`. Useful when
    debugging why a card fails: the raw oracle text and the preprocessed
    form often diverge in subtle ways (~ substitution, contraction
    expansion, quoted-period sentinel)."""

    line: int
    column: int
    pos_in_stream: int

    unexpected: str
    """For ``UnexpectedToken`` the token string; for ``UnexpectedCharacters``
    the offending character; ``"<EOF>"`` for ``UnexpectedEOF``."""

    expected: tuple[str, ...]
    """Sorted terminal / rule names Lark would have accepted at the failure
    point. Empty tuple if the exception didn't expose a candidate set."""

    context: str
    """Output of ``e.get_context(preprocessed_text)`` - the input around the
    failure column with a ``^`` marker. Already multi-line."""

    raw_message: str
    """Full ``str(exc)`` from Lark, including all of the above pre-formatted.
    Useful as a fallback when the structured fields are empty."""


@dataclass(frozen=True, slots=True)
class ParseError:
    """Surface-level failure record returned by :func:`parse`."""

    kind: str  # "incomplete", "invalid", "ambiguous"
    message: str
    position: int | None = None
    details: ParseErrorDetails | None = None
    """Rich Lark exception data for ``parse-error:`` failures. ``None`` for
    ``unmodeled-rule:`` (transformer) and ``lark-error:`` (other Lark
    failures) which don't carry the same UnexpectedInput shape."""


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Result of :func:`parse`: either an ``ast`` or an ``error``."""

    ast: Card | None = None
    error: ParseError | None = None

    @property
    def ok(self) -> bool:
        return self.ast is not None


# ---------------------------------------------------------------------------
# Keyword rule-name -> Keyword enum
# ---------------------------------------------------------------------------

# The grammar emits a rule like ``kwflying`` for each marker keyword. For
# Reed's parametric keywords (equip, ward, cycling, ...) we don't go through
# this table - those have dedicated transformer methods that build the
# specific dataclass.
_SIMPLE_KEYWORD_BY_RULE: dict[str, Keyword] = {
    "kwdeathtouch": Keyword.DEATHTOUCH,
    "kwdefender": Keyword.DEFENDER,
    "kwdoublestrike": Keyword.DOUBLE_STRIKE,
    "kwfirststrike": Keyword.FIRST_STRIKE,
    "kwflash": Keyword.FLASH,
    "kwflying": Keyword.FLYING,
    "kwhaste": Keyword.HASTE,
    "kwindestructible": Keyword.INDESTRUCTIBLE,
    "kwintimidate": Keyword.INTIMIDATE,
    "kwlifelink": Keyword.LIFELINK,
    "kwmenace": Keyword.MENACE,
    "kwreach": Keyword.REACH,
    "kwshroud": Keyword.SHROUD,
    "kwtrample": Keyword.TRAMPLE,
    "kwvigilance": Keyword.VIGILANCE,
    "kwfear": Keyword.FEAR,
    "kwshadow": Keyword.SHADOW,
    "kwhorsemanship": Keyword.HORSEMANSHIP,
    "kwprowess": Keyword.PROWESS,
    "kwchangeling": Keyword.CHANGELING,
    "kwphasing": Keyword.PHASING,
    "kwconspire": Keyword.CONSPIRE,
    "kwpersist": Keyword.PERSIST,
    "kwwither": Keyword.WITHER,
    "kwretrace": Keyword.RETRACE,
    "kwexalted": Keyword.EXALTED,
    "kwcascade": Keyword.CASCADE,
    "kwrebound": Keyword.REBOUND,
    "kwtotemarmor": Keyword.TOTEM_ARMOR,
    "kwinfect": Keyword.INFECT,
    "kwbattlecry": Keyword.BATTLE_CRY,
    "kwlivingweapon": Keyword.LIVING_WEAPON,
    "kwundying": Keyword.UNDYING,
    "kwsoulbond": Keyword.SOULBOND,
    "kwunleash": Keyword.UNLEASH,
    "kwcipher": Keyword.CIPHER,
    "kwevolve": Keyword.EVOLVE,
    "kwextort": Keyword.EXTORT,
    "kwfuse": Keyword.FUSE,
    "kwdethrone": Keyword.DETHRONE,
    "kwexploit": Keyword.EXPLOIT,
    "kwdevoid": Keyword.DEVOID,
    "kwingest": Keyword.INGEST,
    "kwmyriad": Keyword.MYRIAD,
    "kwskulk": Keyword.SKULK,
    "kwmelee": Keyword.MELEE,
    "kwimprovise": Keyword.IMPROVISE,
    "kwaftermath": Keyword.AFTERMATH,
    "kwascend": Keyword.ASCEND,
    "kwassist": Keyword.ASSIST,
    "kwmentor": Keyword.MENTOR,
    "kwhideaway": Keyword.HIDEAWAY,
    "kwepic": Keyword.EPIC,
    "kwhaunt": Keyword.HAUNT,
    "kwfrenzy": Keyword.FRENZY,
    "kwauraswap": Keyword.AURA_SWAP,
    "kwsplitsecond": Keyword.SPLIT_SECOND,
    "kwgraft": Keyword.GRAFT,
    "kwconvoke": Keyword.CONVOKE,
    "kwstorm": Keyword.STORM,
    "kwsunburst": Keyword.SUNBURST,
    "kwdelve": Keyword.DELVE,
    "kwgravestorm": Keyword.GRAVESTORM,
    "kwprovoke": Keyword.PROVOKE,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_keywordlist(items: list[Any]) -> list[Any]:
    """``keywordsequence`` recurses left, so flatten its mixed children."""
    out: list[Any] = []
    for it in items:
        if isinstance(it, list):
            out.extend(_flatten_keywordlist(it))
        else:
            out.append(it)
    return out


def _strip_punct(s: str) -> str:
    """Trim a trailing ``.`` that Lark sometimes preserves on the last token."""
    return s.rstrip(".")


# ---------------------------------------------------------------------------
# Transformer
# ---------------------------------------------------------------------------


class CardTransformer(Transformer):
    """Lark Transformer that emits argentum-press's dataclass AST.

    A handful of rules below intentionally let unmodeled cases fall through
    to :meth:`__default__`, which raises :class:`LoweringIncomplete`. That
    keeps the per-rule methods readable: each one only handles the shape
    we've actually seen.
    """

    # -- Top-level ----------------------------------------------------------

    def cardtext(self, items):
        # cardtext : remindertext? (ability NEWLINE*)*
        lines: list[Any] = []
        for it in items:
            if isinstance(it, ReminderText):
                # Reed attached a leading reminder text to nothing in
                # particular; we drop it on day one.
                continue
            if isinstance(it, list):
                lines.extend(it)
            else:
                lines.append(it)
        return Card(text_box=TextBox(lines=tuple(lines)), abilities=tuple(lines))

    def remindertext(self, items):
        token = items[0]
        return ReminderText(text=str(token))

    # -- Abilities ----------------------------------------------------------

    def ability(self, items):
        # ability : abilityword? statementblock remindertext?  -> regularability
        #         | keywordlist remindertext?
        # The "regularability" arm is fired below; here we handle the
        # keywordlist arm. It returns a *list* of abilities so cardtext
        # can splice them in.
        reminder: ReminderText | None = None
        keyword_abilities: list[Any] = []
        for it in items:
            if isinstance(it, ReminderText):
                reminder = it
            elif isinstance(it, list):
                keyword_abilities.extend(it)
            elif isinstance(it, KeywordAbility):
                keyword_abilities.append(it)
            else:
                # Shouldn't happen, but guard.
                keyword_abilities.append(it)
        if reminder is not None and keyword_abilities:
            # Attach reminder to the final keyword (per Reed's convention).
            last = keyword_abilities[-1]
            keyword_abilities[-1] = _attach_reminder(last, reminder)
        return keyword_abilities

    def regularability(self, items):
        # abilityword? statementblock remindertext?
        ability_word: AbilityWord | None = None
        block: StatementBlock | None = None
        reminder: ReminderText | None = None
        for it in items:
            if isinstance(it, StatementBlock):
                block = it
            elif isinstance(it, ReminderText):
                reminder = it
            elif isinstance(it, AbilityWord):
                ability_word = it
        if block is None:
            raise LoweringIncomplete("regularability-without-block")
        # Promote conditional statements into TriggeredAbility surface.
        triggered = _try_promote_triggered(block)
        if triggered is not None:
            outcome, condition_stmt = triggered
            return TriggeredAbility(
                condition=condition_stmt,
                outcome=outcome,
                ability_word=ability_word,
                reminder_text=reminder,
            )
        return RegularAbility(block=block, ability_word=ability_word, reminder_text=reminder)

    def abilityword(self, items):
        # abilityword: WORD+ "—"  -- items are WORD tokens.
        word = " ".join(str(t) for t in items)
        return AbilityWord(word=word)

    # -- Keyword list -------------------------------------------------------

    def keywordlist(self, items):
        return _flatten_keywordlist(items)

    def keywordsequence(self, items):
        return _flatten_keywordlist(items)

    def keywordability(self, items):
        return items[0]

    # -- Simple-marker keywords --------------------------------------------
    # All these collapse to SimpleKeywordAbility(keyword=<enum>).

    def _simple_kw(self, rule_name: str):
        kw = _SIMPLE_KEYWORD_BY_RULE.get(rule_name)
        if kw is None:
            raise LoweringIncomplete(f"unmapped-simple-kw:{rule_name}")
        return SimpleKeywordAbility(keyword=kw)

    def kwdeathtouch(self, items):
        return self._simple_kw("kwdeathtouch")

    def kwdefender(self, items):
        return self._simple_kw("kwdefender")

    def kwdoublestrike(self, items):
        return self._simple_kw("kwdoublestrike")

    def kwfirststrike(self, items):
        return self._simple_kw("kwfirststrike")

    def kwflash(self, items):
        return self._simple_kw("kwflash")

    def kwflying(self, items):
        return self._simple_kw("kwflying")

    def kwhaste(self, items):
        return self._simple_kw("kwhaste")

    def kwindestructible(self, items):
        return self._simple_kw("kwindestructible")

    def kwintimidate(self, items):
        return self._simple_kw("kwintimidate")

    def kwlifelink(self, items):
        return self._simple_kw("kwlifelink")

    def kwmenace(self, items):
        return self._simple_kw("kwmenace")

    def kwreach(self, items):
        return self._simple_kw("kwreach")

    def kwshroud(self, items):
        return self._simple_kw("kwshroud")

    def kwtrample(self, items):
        return self._simple_kw("kwtrample")

    def kwvigilance(self, items):
        return self._simple_kw("kwvigilance")

    def kwfear(self, items):
        return self._simple_kw("kwfear")

    def kwshadow(self, items):
        return self._simple_kw("kwshadow")

    def kwhorsemanship(self, items):
        return self._simple_kw("kwhorsemanship")

    def kwprowess(self, items):
        return self._simple_kw("kwprowess")

    def kwchangeling(self, items):
        return self._simple_kw("kwchangeling")

    def kwphasing(self, items):
        return self._simple_kw("kwphasing")

    def kwconspire(self, items):
        return self._simple_kw("kwconspire")

    def kwpersist(self, items):
        return self._simple_kw("kwpersist")

    def kwwither(self, items):
        return self._simple_kw("kwwither")

    def kwretrace(self, items):
        return self._simple_kw("kwretrace")

    def kwexalted(self, items):
        return self._simple_kw("kwexalted")

    def kwcascade(self, items):
        return self._simple_kw("kwcascade")

    def kwrebound(self, items):
        return self._simple_kw("kwrebound")

    def kwtotemarmor(self, items):
        return self._simple_kw("kwtotemarmor")

    def kwinfect(self, items):
        return self._simple_kw("kwinfect")

    def kwbattlecry(self, items):
        return self._simple_kw("kwbattlecry")

    def kwlivingweapon(self, items):
        return self._simple_kw("kwlivingweapon")

    def kwundying(self, items):
        return self._simple_kw("kwundying")

    def kwsoulbond(self, items):
        return self._simple_kw("kwsoulbond")

    def kwunleash(self, items):
        return self._simple_kw("kwunleash")

    def kwcipher(self, items):
        return self._simple_kw("kwcipher")

    def kwevolve(self, items):
        return self._simple_kw("kwevolve")

    def kwextort(self, items):
        return self._simple_kw("kwextort")

    def kwfuse(self, items):
        return self._simple_kw("kwfuse")

    def kwdethrone(self, items):
        return self._simple_kw("kwdethrone")

    def kwexploit(self, items):
        return self._simple_kw("kwexploit")

    def kwdevoid(self, items):
        return self._simple_kw("kwdevoid")

    def kwingest(self, items):
        return self._simple_kw("kwingest")

    def kwmyriad(self, items):
        return self._simple_kw("kwmyriad")

    def kwskulk(self, items):
        return self._simple_kw("kwskulk")

    def kwmelee(self, items):
        return self._simple_kw("kwmelee")

    def kwimprovise(self, items):
        return self._simple_kw("kwimprovise")

    def kwaftermath(self, items):
        return self._simple_kw("kwaftermath")

    def kwascend(self, items):
        return self._simple_kw("kwascend")

    def kwassist(self, items):
        return self._simple_kw("kwassist")

    def kwmentor(self, items):
        return self._simple_kw("kwmentor")

    def kwhideaway(self, items):
        return self._simple_kw("kwhideaway")

    def kwepic(self, items):
        return self._simple_kw("kwepic")

    def kwhaunt(self, items):
        return self._simple_kw("kwhaunt")

    def kwfrenzy(self, items):
        return self._simple_kw("kwfrenzy")

    def kwauraswap(self, items):
        return self._simple_kw("kwauraswap")

    def kwsplitsecond(self, items):
        return self._simple_kw("kwsplitsecond")

    def kwgraft(self, items):
        return self._simple_kw("kwgraft")

    def kwconvoke(self, items):
        return self._simple_kw("kwconvoke")

    def kwstorm(self, items):
        return self._simple_kw("kwstorm")

    def kwsunburst(self, items):
        return self._simple_kw("kwsunburst")

    def kwdelve(self, items):
        return self._simple_kw("kwdelve")

    def kwgravestorm(self, items):
        return self._simple_kw("kwgravestorm")

    def kwprovoke(self, items):
        return self._simple_kw("kwprovoke")

    # -- Parametric keywords -----------------------------------------------

    def kwequip(self, items):
        # "equip" cost | "equip" genericdescriptionexpression cost
        if len(items) == 1:
            return EquipAbility(cost=items[0])
        return EquipAbility(quality=items[0], cost=items[1])

    def kwenchant(self, items):
        return EnchantAbility(descriptor=items[0])

    def kwhexproof(self, items):
        # "hexproof" | "hexproof" "from" genericdescriptionexpression
        if not items:
            # Plain hexproof - prefer the SimpleKeywordAbility shape.
            return SimpleKeywordAbility(keyword=Keyword.HEXPROOF)
        return HexproofAbility(quality=items[0])

    def kwlandwalk(self, items):
        return LandwalkAbility(landtype=items[0])

    def kwprotection(self, items):
        return ProtectionAbility(qualities=tuple(items))

    def kwward(self, items):
        return WardAbility(cost=items[0])

    def kwbanding(self, items):
        if not items:
            return SimpleKeywordAbility(keyword=Keyword.BANDING)
        return BandingAbility(quality=items[0])

    def kwrampage(self, items):
        return RampageAbility(caliber=items[0])

    def kwcumulativeupkeep(self, items):
        return CumulativeUpkeepAbility(cost=items[0])

    def kwbuyback(self, items):
        return BuybackAbility(cost=items[0])

    def kwcycling(self, items):
        # [typeexpression] "cycling" cost
        if len(items) == 2:
            return CyclingAbility(cycling_type=items[0], cost=items[1])
        return CyclingAbility(cost=items[0])

    def kwecho(self, items):
        return EchoAbility(cost=items[0])

    def kwfading(self, items):
        return FadingAbility(caliber=items[0])

    def kicker(self, items):
        return KickerAbility(cost=items[0], is_multi=False)

    def multikicker(self, items):
        return KickerAbility(cost=items[0], is_multi=True)

    def kwflashback(self, items):
        return FlashbackAbility(cost=items[0])

    def kwmadness(self, items):
        return MadnessAbility(cost=items[0])

    def kwmorph(self, items):
        return MorphAbility(cost=items[0], is_mega=False)

    def kwmegamorph(self, items):
        return MorphAbility(cost=items[0], is_mega=True)

    def kwamplify(self, items):
        return AmplifyAbility(caliber=items[0])

    def kwaffinity(self, items):
        return AffinityAbility(descriptor=items[0])

    def kwentwine(self, items):
        return EntwineAbility(cost=items[0])

    def kwmodular(self, items):
        return ModularAbility(caliber=items[0])

    def kwbushido(self, items):
        return BushidoAbility(caliber=items[0])

    def kwsoulshift(self, items):
        return SoulshiftAbility(caliber=items[0])

    def kwsplice(self, items):
        return SpliceAbility(splice_type=items[0], cost=items[1])

    def kwoffering(self, items):
        return OfferingAbility(descriptor=items[0])

    def kwninjutsu(self, items):
        return NinjutsuAbility(cost=items[0])

    def kwdredge(self, items):
        return DredgeAbility(caliber=items[0])

    def kwtransmute(self, items):
        return TransmuteAbility(cost=items[0])

    def kwbloodthirst(self, items):
        return BloodthirstAbility(caliber=items[0])

    def kwreplicate(self, items):
        return ReplicateAbility(cost=items[0])

    def kwrecover(self, items):
        return RecoverAbility(cost=items[0])

    def kwripple(self, items):
        return RippleAbility(caliber=items[0])

    def kwsuspend(self, items):
        return SuspendAbility(caliber=items[0], cost=items[1])

    def kwvanishing(self, items):
        if not items:
            return VanishingAbility()
        return VanishingAbility(caliber=items[0])

    def kwabsorb(self, items):
        return AbsorbAbility(caliber=items[0])

    def kwfortify(self, items):
        return FortifyAbility(cost=items[0])

    def kwpoisonous(self, items):
        return PoisonousAbility(caliber=items[0])

    def kwtransfigure(self, items):
        return TransfigureAbility(cost=items[0])

    def kwchampion(self, items):
        return ChampionAbility(descriptor=items[0])

    def kwevoke(self, items):
        return EvokeAbility(cost=items[0])

    def kwprowl(self, items):
        return ProwlAbility(cost=items[0])

    def kwreinforce(self, items):
        # "reinforce" cost — Reed's grammar has no caliber here; the cost
        # is the only operand we get. We synthesize a placeholder caliber
        # rather than diverging from the dataclass; this is BLB-day-one.
        return ReinforceAbility(caliber=NumberValue(value="x", ntype=NumberTypeEnum.CUSTOM), cost=items[0])

    def kwdevour(self, items):
        return DevourAbility(caliber=items[0])

    def kwunearth(self, items):
        return UnearthAbility(cost=items[0])

    def kwannihilator(self, items):
        return AnnihilatorAbility(caliber=items[0])

    def kwlevelup(self, items):
        return LevelUpAbility(cost=items[0])

    def kwmiracle(self, items):
        return MiracleAbility(cost=items[0])

    def kwoverload(self, items):
        return OverloadAbility(cost=items[0])

    def kwscavenge(self, items):
        return ScavengeAbility(cost=items[0])

    def kwbestow(self, items):
        return BestowAbility(cost=items[0])

    def kwtribute(self, items):
        return TributeAbility(caliber=items[0])

    def kwhiddenagenda(self, items):
        return HiddenAgendaAbility(is_double_agenda=False)

    def kwdoubleagenda(self, items):
        return HiddenAgendaAbility(is_double_agenda=True)

    def kwoutlast(self, items):
        return OutlastAbility(cost=items[0])

    def kwdash(self, items):
        return DashAbility(cost=items[0])

    def kwrenown(self, items):
        return RenownAbility(caliber=items[0])

    def kwawaken(self, items):
        return AwakenAbility(caliber=items[0], cost=items[1])

    def kwsurge(self, items):
        return SurgeAbility(cost=items[0])

    def kwemerge(self, items):
        return EmergeAbility(cost=items[0])

    def kwescalate(self, items):
        return EscalateAbility(cost=items[0])

    def kwcrew(self, items):
        return CrewAbility(caliber=items[0])

    def kwfabricate(self, items):
        return FabricateAbility(caliber=items[0])

    def kwpartner(self, items):
        if not items:
            return SimpleKeywordAbility(keyword=Keyword.PARTNER)
        # objectname token comes through as a Name-like.
        name = items[0]
        if isinstance(name, Name):
            return PartnerAbility(partner_name=name)
        return PartnerAbility(partner_name=Name(name=str(name)))

    def kwembalm(self, items):
        return EmbalmAbility(cost=items[0])

    def kweternalize(self, items):
        return EternalizeAbility(cost=items[0])

    def kwafflict(self, items):
        return AfflictAbility(caliber=items[0])

    def kwsurveil(self, items):
        return SurveilAbility(caliber=items[0])

    def kwjumpstart(self, items):
        return JumpStartAbility(cost=items[0])

    def kwwebslinging(self, items):
        return WebSlingingAbility(cost=items[0])

    def kwmayhem(self, items):
        return MayhemAbility(cost=items[0])

    # -- Cost ---------------------------------------------------------------

    def cost(self, items):
        return items[0]

    def costsequence(self, items):
        return CostSequenceExpression(arguments=tuple(items))

    def dashcostexpression(self, items):
        # DASH ( manasymbolexpression | effectexpression ) ("," effectexpression)*
        # The DASH token is preserved in items[0]; we drop it.
        ops = [it for it in items if not (isinstance(it, Token) and str(it) == "—")]
        if len(ops) == 1:
            return DashCostExpression(cost=ops[0])
        return DashCostExpression(cost=CostSequenceExpression(arguments=tuple(ops)))

    # -- Value expressions -------------------------------------------------

    def valueexpression(self, items):
        return items[0]

    def valueterm(self, items):
        return items[0]

    def valuenumber(self, items):
        token = items[0]
        text = _strip_punct(str(token))
        try:
            return NumberValue(value=int(text), ntype=NumberTypeEnum.LITERAL)
        except ValueError as e:
            raise LoweringIncomplete(f"non-int-valuenumber:{token}") from e

    def valuecardinal(self, items):
        # "one"|"two"|... - keep as a string-tagged NumberValue.
        word = str(items[0]) if items else ""
        return NumberValue(value=word, ntype=NumberTypeEnum.CARDINAL)

    def valueordinal(self, items):
        word = str(items[0]) if items else ""
        return NumberValue(value=word, ntype=NumberTypeEnum.ORDINAL)

    def valuefrequency(self, items):
        word = str(items[0]) if items else ""
        return NumberValue(value=word, ntype=NumberTypeEnum.FREQUENCY)

    def thatmanyexpression(self, items):
        # `!thatmanyexpression: valuefrequency? "that" ("much"|"many")`. The
        # `!` keeps the "that"/"much"/"many" tokens; we surface "that many" as a
        # CUSTOM NumberValue so it slots into the valueexpression path.
        return NumberValue(value="that many", ntype=NumberTypeEnum.CUSTOM)

    def uptoexpression(self, items):
        # `uptoexpression: "up" "to" valueterm`. Surface as a CUSTOM NumberValue
        # carrying the inner value (e.g. "up to 2") so it slots into the
        # valueexpression path.
        inner = items[0]
        inner_str = inner.value if isinstance(inner, NumberValue) else str(inner)
        return NumberValue(value=f"up to {inner_str}", ntype=NumberTypeEnum.CUSTOM)

    def lteqexpression(self, items):
        # `lteqexpression: effectexpression? (valueexpression "or" ("less" | "fewer")
        #                  | "less" "than" "or" "equal" "to" valueexpression)`.
        # Anonymous string terminals are filtered, so both arms produce the
        # same item shape: [valueexpression] or [effectexpression, valueexpression].
        if len(items) == 1:
            return ValueLtEqExpression(lhs=None, rhs=items[0])
        return ValueLtEqExpression(lhs=items[0], rhs=items[1])

    def gteqexpression(self, items):
        # `gteqexpression: effectexpression? (valueexpression "or" ("greater" | "more")
        #                  | "greater" "than" "or" "equal" "to" valueexpression)`.
        # Mirrors lteqexpression: anonymous string terminals are filtered, so
        # both arms produce [valueexpression] or [effectexpression, valueexpression].
        if len(items) == 1:
            return ValueGtEqExpression(lhs=None, rhs=items[0])
        return ValueGtEqExpression(lhs=items[0], rhs=items[1])

    def valuecustom(self, items):
        # "x" or "*" - empty items because the grammar matches a literal.
        # The actual character is recoverable from the parse tree only if
        # we look at the parent context. For BLB the only valuecustom we
        # care about is X (Mind Spring).
        return NumberValue(value="x", ntype=NumberTypeEnum.CUSTOM)

    # -- ptchange ----------------------------------------------------------

    def ptchangeexpression(self, items):
        # (PLUS|MINUS) valueterm "/" (PLUS|MINUS) valueterm
        # Children come through as four items: sign, value, sign, value.
        signs: list[str] = []
        values: list[NumberValue] = []
        for it in items:
            if isinstance(it, Token):
                signs.append(str(it))
            elif isinstance(it, NumberValue):
                values.append(it)
        if len(signs) != 2 or len(values) != 2:
            raise LoweringIncomplete(
                f"ptchange-unexpected-shape:signs={signs},values={len(values)}"
            )
        power = _signed_number(signs[0], values[0])
        toughness = _signed_number(signs[1], values[1])
        return PTExpression(power=power, toughness=toughness)

    def ptexpression(self, items):
        return PTExpression(power=items[0], toughness=items[1])

    # -- Declarations / references -----------------------------------------

    def declarationorreference(self, items):
        return items[0]

    def genericdeclarationexpression(self, items):
        return items[0]

    def eachofgenericdeclarationexpression(self, items):
        # "each of" <declarationorreference> — mirrors the eachdecorator path
        # (transformer.py:eachdecorator) so downstream sees a UnaryOp shape.
        return EachExpression(operand=items[0])

    def genericdescriptionexpression(self, items):
        return items[0]

    def objectdeclaration(self, items):
        # declarationdecorator* objectdefinition
        modifiers = [it for it in items[:-1] if it is not None]
        defn = items[-1]
        if not modifiers:
            return defn
        return GenericDeclarationExpression(definition=_wrap_modifiers(defn, modifiers))

    def objectdefinition(self, items):
        return items[0]

    def objectdescriptionexpression(self, items):
        # objectpreterm+ objectpostterm*
        return DescriptionExpression(descriptors=tuple(items))

    def orobjectdescriptionexpression(self, items):
        # objectdescriptionexpression ("," objectdescriptionexpression ",")* "or" objectdescriptionexpression
        result = items[-1]
        for item in reversed(items[:-1]):
            result = OrExpression(lhs=item, rhs=result)
        return result

    def andobjectdescriptionexpression(self, items):
        # objectdescriptionexpression ("," objectdescriptionexpression ",")* "and" objectdescriptionexpression
        result = items[-1]
        for item in reversed(items[:-1]):
            result = AndExpression(lhs=item, rhs=result)
        return result

    def andorobjectdescriptionexpression(self, items):
        # objectdescriptionexpression ("," objectdescriptionexpression ",")* "and/or" objectdescriptionexpression
        result = items[-1]
        for item in reversed(items[:-1]):
            result = AndOrExpression(lhs=item, rhs=result)
        return result

    def objectpreterm(self, items):
        return items[0]

    def objectpostterm(self, items):
        return items[0]

    def playerdeclaration(self, items):
        modifiers = [it for it in items[:-1] if it is not None]
        defn = items[-1]
        if not modifiers:
            return defn
        return GenericDeclarationExpression(definition=_wrap_modifiers(defn, modifiers))

    def playerdefinition(self, items):
        return items[0]

    def playerdescriptionexpression(self, items):
        return DescriptionExpression(descriptors=tuple(items))

    def playerdescriptionterm(self, items):
        return items[0]

    def playerterm(self, items):
        # Reed had PlayerTerm dataclass; we return a NumberValue-like token
        # as the simplest carrier. Today we just pass through the token
        # value as a NameReference so downstream code can read text.
        token = items[0]
        return NameReference(antecedent=Name(name=str(token)))

    def playerdeclref(self, items):
        return items[0]

    def playerreference(self, items):
        # referencedecorator+ playerdefinition - we just attach the
        # decorators by wrapping in DescriptionExpression.
        return DescriptionExpression(descriptors=tuple(items))

    def objectdeclref(self, items):
        return items[0]

    def objectreference(self, items):
        return DescriptionExpression(descriptors=tuple(items))

    # -- Decorators --------------------------------------------------------

    def declarationdecorator(self, items):
        return items[0]

    def referencedecorator(self, items):
        return items[0]

    def controlmodifier(self, items):
        # "under <ref> control" — surface marker on enterzoneexpression
        # ("entered the battlefield under your control"). The parent doesn't
        # consume it; downstream only cares about presence.
        return None

    def attachedmodifier(self, items):
        # "attached (only? to <ref>)?" — surface marker mirroring
        # controlmodifier; the parent modifier rule doesn't consume it.
        return None

    def targetdecorator(self, items):
        # "target" or "<value> target"
        if not items:
            return TargetExpression(operand=None, is_any=False)
        return TargetExpression(operand=items[0], is_any=False)

    def eachdecorator(self, items):
        return EachExpression(operand=None) if not items else EachExpression(operand=items[0])

    def alldecorator(self, items):
        return None  # passthrough; objectdeclaration absorbs.

    def otherdecorator(self, items):
        return None

    def indefinitearticledecorator(self, items):
        return None

    def definitearticledecorator(self, items):
        return None

    def anydecorator(self, items):
        return None

    def samedecorator(self, items):
        return None

    def thatreference(self, items):
        return None

    def thisreference(self, items):
        return None

    def possessivereference(self, items):
        return None

    def possessiveterm(self, items):
        # `!possessiveterm: "its" | "your" | "their" | <ref>("'s"|"'") | ...`
        # Surface-only marker; downstream (zonedeclarationexpression, time*) is
        # the consumer and only cares about presence, not the literal owner.
        return None

    # -- Reference terms ---------------------------------------------------

    def reference(self, items):
        return items[0]

    def neutralreference(self, items):
        return ItReference()

    def selfreference(self, items):
        return SelfReference()

    def namereference(self, items):
        return NameReference(antecedent=None)

    def anytargetexpression(self, items):
        return TargetExpression(operand=None, is_any=True)

    # -- Types -------------------------------------------------------------

    def typeexpression(self, items):
        return TypeExpression(types=tuple(items), comma_delimited=False)

    def ortypeexpression(self, items):
        return TypeExpression(types=tuple(items), comma_delimited=True)

    def typeterm(self, items):
        # Pass the raw token through; downstream code reads its value.
        token = items[0]
        return Name(name=str(token)) if isinstance(token, Token) else token

    def nontypeterm(self, items):
        return NonExpression(operand=items[0])

    # -- Modifiers / qualifiers -------------------------------------------

    def modifier(self, items):
        # We don't model the full modifier taxonomy yet - pass through as
        # an opaque Name so DescriptionExpression can carry the surface text.
        token = items[0]
        return Name(name=str(token))

    def qualifier(self, items):
        token = items[0]
        return Name(name=str(token))

    def characteristicexpression(self, items):
        return items[0]

    def characteristicvaluecompexpr(self, items):
        # `characteristicterms (valueexpression|ptexpression)` in either order
        # — e.g. "mana value 2 or less". Wrap both children in a
        # DescriptionExpression so it slots into the characteristicexpression
        # path the same way the bare characteristicterms variant does.
        return DescriptionExpression(descriptors=tuple(items))

    def characteristicterms(self, items):
        return items[0]

    def characteristicterm(self, items):
        # `modifier* characteristic` — modifiers are already lowered to
        # Name pass-throughs; we don't model them on characteristics yet.
        return items[-1]

    def characteristicpossessiveexpr(self, items):
        # `possessiveterm+ characteristicterm` — possessiveterm is a
        # surface-only marker (returns None); only the characteristic carries
        # downstream meaning, mirroring `characteristicterm` above.
        return items[-1]

    def characteristicthereference(self, items):
        # `"the" characteristicterm` — "the" is a surface-only marker; only
        # the characteristic carries downstream meaning.
        return items[-1]

    def characteristicandexpr(self, items):
        result = items[0]
        for item in items[1:]:
            result = AndExpression(lhs=result, rhs=item)
        return result

    def characteristic(self, items):
        # OBJECTCHARACTERISTIC | PLAYERCHARACTERISTIC token — pass through
        # as an opaque Name (same shape as modifier/qualifier).
        return Name(name=str(items[0]))

    def colorexpression(self, items):
        return items[0]

    def colorterm(self, items):
        return ColorExpression(value=Name(name=str(items[0])))

    def colorsingleexpr(self, items):
        return ColorExpression(value=items[0])

    def colorandexpr(self, items):
        result = items[0]
        for item in items[1:]:
            result = AndExpression(lhs=result, rhs=item)
        return ColorExpression(value=result)

    # -- Withexpression / namedexpression ---------------------------------

    def withexpression(self, items):
        return WithExpression(operand=items[0]) if items else WithExpression(operand=Name(name=""))

    def withoutexpression(self, items):
        # No dedicated WithoutExpression - reuse WithExpression w/ a marker.
        # Day-one stand-in.
        return WithExpression(operand=items[0]) if items else WithExpression(operand=Name(name=""))

    def ofexpression(self, items):
        # `"of" declarationorreference` postterm — e.g. "spells of the chosen
        # type". Reuse WithExpression as a day-one stand-in.
        return WithExpression(operand=items[0]) if items else WithExpression(operand=Name(name=""))

    # -- Object-postterm postfixes -----------------------------------------

    def controlpostfix(self, items):
        # `<playerdeclref> control[s]` — e.g. "creatures you control".
        return ControlExpression(controller=items[0])

    def negativecontrolpostfix(self, items):
        # `<playerdeclref> do/does not control` — e.g. "creatures you don't control".
        return NonExpression(operand=ControlExpression(controller=items[0]))

    def ownpostfix(self, items):
        # `<playerdeclref> own[s]` — possession variant of controlpostfix.
        return ControlExpression(controller=items[0])

    def negativeownpostfix(self, items):
        return NonExpression(operand=ControlExpression(controller=items[0]))

    def namedexpression(self, items):
        return NamedExpression(operand=items[0])

    def objectname(self, items):
        return Name(name=str(items[0]))

    def OBJECTNAME(self, token):
        return Name(name=str(token))

    # -- Statements --------------------------------------------------------

    def statementblock(self, items):
        return StatementBlock(statements=tuple(items))

    def statement(self, items):
        return items[0]

    def expressionstatement(self, items):
        # (effectexpression | beexpression | valueexpression) timeexpression?
        return ExpressionStatement(root=items[0])

    def maystatement(self, items):
        # playerdeclref? ("may" | "may" "have") statement
        if len(items) == 2:
            return MayStatement(player=items[0], statement=items[1])
        # Implied "you"
        return MayStatement(
            player=NameReference(antecedent=Name(name="you")),
            statement=items[0],
        )

    def thenstatement(self, items):
        return CompoundStatement(statements=(items[0],), terminator=CompoundTerminator.THEN)

    def insteadstatement(self, items):
        return CompoundStatement(statements=(items[0],), terminator=CompoundTerminator.INSTEAD)

    def modalchoiceexpression(self, items):
        # MODALCHOICE abilityword? statementblock
        ability_word: AbilityWord | None = None
        block: StatementBlock | None = None
        for it in items:
            if isinstance(it, StatementBlock):
                block = it
            elif isinstance(it, AbilityWord):
                ability_word = it
        if block is None:
            raise LoweringIncomplete("modalchoice-without-block")
        return ModalChoice(block=block, ability_word=ability_word)

    def modalstatement(self, items):
        # "choose" valuecardinal DASH (modalchoiceexpression)+
        # DASH token is preserved in items; filter it out.
        rest = [it for it in items if not (isinstance(it, Token) and str(it) == "—")]
        number_of_choices = rest[0]
        options = tuple(rest[1:])
        return ModalExpression(number_of_choices=number_of_choices, options=options)

    def activationstatement(self, items):
        # `<cost> : <statementblock>` — body of an activated ability.
        return ActivationStatement(cost=items[0], instructions=items[1])

    def activationrestrictionstatement(self, items):
        # `"activate" "only" "as" "a" "sorcery"` — grammar has a single form,
        # all keywords, no children. Return a bare marker.
        return ActivationRestrictionStatement()

    def triggerrestrictionstatement(self, items):
        # `declarationorreference? "trigger"["s"] "only" valuefrequency timeexpression?`.
        # Literal "trigger"/"only" are dropped; the NumberValue (valuefrequency) is
        # always present and splits the optional subject from the optional time.
        freq_idx = next(
            (i for i, it in enumerate(items) if isinstance(it, NumberValue)), None
        )
        if freq_idx is None:
            raise LoweringIncomplete("triggerrestriction-without-frequency")
        subject = items[0] if freq_idx == 1 else None
        time = items[freq_idx + 1] if freq_idx + 1 < len(items) else None
        return TriggerRestrictionStatement(
            frequency=items[freq_idx], subject=subject, time=time
        )

    def abilitysequencestatement(self, items):
        # `flying`, `flying and haste`, `flying, vigilance, and trample`.
        # Grammar yields one or more keyword abilities separated by `,` / `and`.
        return AbilitySequenceStatement(abilities=tuple(items))

    def beingstatement(self, items):
        # Passthrough — beingstatement is the union (is/has/isnt/can/becomes/
        # costchange/where), each of which has its own handler that returns
        # the concrete dataclass.
        return items[0]

    def hasstatement(self, items):
        # `<subject>? has <abilities-or-characteristic>` -> BeingStatement.
        # The trailing item is always the RHS; an optional declref leads when
        # the subject is explicit (e.g. "Other creatures you control have prowess.").
        if len(items) == 1:
            return BeingStatement(rhs=items[0])
        return BeingStatement(lhs=items[0], rhs=items[1])

    def hasquotedstatement(self, items):
        # `<subject>? has "<statementblock>"` -> BeingStatement with the granted
        # ability body as RHS (e.g. Enchanted land has "{1}, {T}: ...").
        if len(items) == 1:
            return BeingStatement(rhs=items[0])
        return BeingStatement(lhs=items[0], rhs=items[1])

    def hasmixedstatement(self, items):
        non_token = [it for it in items if not isinstance(it, Token)]
        if len(non_token) == 2:
            return BeingStatement(rhs=non_token[1])
        return BeingStatement(lhs=non_token[0], rhs=non_token[-1])

    def isstatement(self, items):
        # `<subject> is/was/are [each] [still|not] <rhs>` -> BeingStatement.
        # The grammar tags this rule with `!`, so the is/was/are/each/still/not
        # tokens come through as Tokens we drop here.
        non_token = [it for it in items if not isinstance(it, Token)]
        if len(non_token) == 1:
            return BeingStatement(rhs=non_token[0])
        return BeingStatement(lhs=non_token[0], rhs=non_token[-1])

    def becomesstatement(self, items):
        # `<subject>? become[s] <rhs>` -> BeingStatement.
        if len(items) == 1:
            return BeingStatement(rhs=items[0])
        return BeingStatement(lhs=items[0], rhs=items[1])

    def costincreasestatement(self, items):
        # `<subject> cost[s] <mana> more to cast/activate` — literal strings
        # are dropped by the grammar, leaving the declref and the mana symbol.
        return CostIncreaseStatement(subject=items[0], amount=items[1])

    def thereexistsstatement(self, items):
        # `there is/are <decl>` — existence claim used as the conditional of
        # an `as long as`/`if` clause (e.g. "as long as there are eight or
        # more cards in your graveyard, …"). Literal "there"/"is"/"are" are
        # dropped by the grammar; only the declaration survives.
        return ThereExistsStatement(subject=items[0])

    # -- Compound statements ----------------------------------------------

    def compoundthenstatement(self, items):
        return CompoundStatement(statements=tuple(items), terminator=CompoundTerminator.THEN)

    def compoundandstatement(self, items):
        return CompoundStatement(statements=tuple(items), terminator=CompoundTerminator.AND)

    def compoundorstatement(self, items):
        return CompoundStatement(statements=tuple(items), terminator=CompoundTerminator.AND)

    def compounduntilstatement(self, items):
        # The grammar shape is:
        #   statement ("," statement)* untilstatement
        # The untilstatement is the *inverted* arm (untiltimestatementinv).
        # We flatten into a CompoundStatement so the surface is preserved.
        if len(items) == 2 and isinstance(items[1], UntilStatement):
            # The until-statement already wraps the verb-phrase + duration;
            # graft the subject onto the until's consequence.
            subject_stmt = items[0]
            until = items[1]
            return UntilStatement(
                conditional=until.conditional,
                consequence=_compound_pair(subject_stmt, until.consequence),
                inverted=until.inverted,
            )
        return CompoundStatement(statements=tuple(items), terminator=CompoundTerminator.AND)

    # -- Conditional statements -------------------------------------------

    def conditionalstatement(self, items):
        return items[0]

    def ifstatement(self, items):
        # "if" statement "," statement
        return IfStatement(conditional=items[0], consequence=items[1], inverted=False)

    def ifstatementinv(self, items):
        # statement "only"? "if" statement
        return IfStatement(conditional=items[1], consequence=items[0], inverted=True)

    def whenstatement(self, items):
        return WhenStatement(conditional=items[0], consequence=items[1], inverted=False)

    def whenstatementinv(self, items):
        return WhenStatement(conditional=items[1], consequence=items[0], inverted=True)

    def wheneverstatement(self, items):
        # "whenever" statement timeexpression? "," statement
        if len(items) == 3:
            conditional, _time, consequence = items
        else:
            conditional, consequence = items
        return WheneverStatement(conditional=conditional, consequence=consequence, inverted=False)

    def wheneverstatementinv(self, items):
        if len(items) == 3:
            consequence, conditional, _time = items
        else:
            consequence, conditional = items
        return WheneverStatement(conditional=conditional, consequence=consequence, inverted=True)

    def asstatement(self, items):
        # "as" statement "," statement — e.g. "As ~ enters, look at...".
        return AsStatement(conditional=items[0], consequence=items[1], inverted=False)

    def exceptstatement(self, items):
        # statement ","? "except" (("by"|"for") genericdeclarationexpression | statement)
        return ExceptStatement(conditional=items[0], consequence=items[1])

    def atstatement(self, items):
        return AtStatement(conditional=items[0], consequence=items[1], inverted=False)

    def atstatementinv(self, items):
        return AtStatement(conditional=items[1], consequence=items[0], inverted=True)

    def aslongasstatement(self, items):
        # "for"? "as" "long" "as" statement "," statement
        return AsLongAsStatement(conditional=items[0], consequence=items[1], inverted=False)

    def aslongasstatementinv(self, items):
        # statement "for"? "as" "long" "as" statement
        return AsLongAsStatement(conditional=items[1], consequence=items[0], inverted=True)

    def forstatementinv(self, items):
        # statement "for" "each" (genericdeclarationexpression | "time" statement) ("beyond" "the" "first")?
        consequence, conditional = items[0], items[1]
        return ForStatement(conditional=conditional, consequence=consequence)

    def untiltimestatement(self, items):
        # "until" timeexpression "," statement
        return UntilStatement(conditional=items[0], consequence=items[1], inverted=False)

    def untiltimestatementinv(self, items):
        # statement "until" timeexpression
        # In the compounduntilstatement context, the inverted arm gets the
        # verb-phrase as items[0] and the timeexpression as items[1].
        return UntilStatement(conditional=items[1], consequence=items[0], inverted=True)

    def untileffecthappensstatement(self, items):
        return UntilStatement(
            conditional=items[0],
            consequence=ExpressionStatement(root=Name(name="")),
            inverted=False,
        )

    # -- Time expressions --------------------------------------------------

    def timeexpression(self, items):
        # We don't model time fully; carry surface text as a Name so any
        # later stage can see what we saw.
        if len(items) == 1:
            return items[0]
        return DescriptionExpression(descriptors=tuple(items))

    def firsttimetimeexpression(self, items):
        return DescriptionExpression(descriptors=(Name(name="for the first time"), *items))

    def timeterm(self, items):
        # Pass through tokens / decorators wrapped in a Name.
        if len(items) == 1 and isinstance(items[0], Token):
            return Name(name=str(items[0]))
        return DescriptionExpression(descriptors=tuple(items))

    def timeendmodifier(self, items):
        return Name(name="end of")

    def timebeginmodifier(self, items):
        return Name(name="beginning of")

    def timemodifier(self, items):
        return items[0]

    def nexttimemodifier(self, items):
        return Name(name="next")

    def additionaltimemodifier(self, items):
        return Name(name="additional")

    def extratimemodifier(self, items):
        return Name(name="extra")

    def startendspecifier(self, items):
        return items[0]

    # -- Zones -------------------------------------------------------------

    def zonedeclarationexpression(self, items):
        return items[-1]

    def zone(self, items):
        # ZONE token -> Name carrying the surface form.
        return Name(name=str(items[0]))

    def locationexpression(self, items):
        # Pass the zone through.
        return items[-1]

    # -- Effect dispatch ---------------------------------------------------

    def effectexpression(self, items):
        return items[0]

    def keywordactionexpression(self, items):
        return items[0]

    def basickeywordaction(self, items):
        return items[0]

    def specialkeywordaction(self, items):
        return items[0]

    # -- Specific effects --------------------------------------------------

    def dealsdamageexpression(self, items):
        # declarationorreference? "deals" valueexpression? DAMAGETYPE
        #   ("to" declarationorreference)? (quantityrulemodification)*
        # Children we care about: origin (declref), amount (value), DAMAGETYPE
        # (a Token), subject (declref). Quantity rule modifiers are dropped.
        origin: Expression | None = None
        amount: Expression | None = None
        damage_type = DamageType(value=DamageTypeEnum.REGULAR)
        subject: Expression | None = None
        seen_dmg = False
        for it in items:
            if isinstance(it, Token):
                ttype = getattr(it, "type", "")
                if ttype == "DAMAGETYPE":
                    damage_type = DamageType(value=_damage_type_for(str(it)))
                    seen_dmg = True
                # Other tokens (quantity modifier words) we ignore.
                continue
            if isinstance(it, NumberValue):
                amount = it
                continue
            # Declaration / reference
            if not seen_dmg and origin is None:
                origin = it
            elif seen_dmg and subject is None:
                subject = it
            else:
                # Spare declaration: stash on amount or skip.
                if amount is None:
                    amount = it
        if origin is None:
            # The implied-antecedent variant ("4 damage to any target")
            # has no origin token.
            origin = NameReference(antecedent=None)
        return DealsDamageExpression(
            origin=origin,
            damage_type=damage_type,
            damage_amount=amount,
            subject=subject,
            variant=DealsDamageVariant.A,
        )

    def destroyexpression(self, items):
        return DestroyExpression(subject=items[0])

    def sacrificeexpression(self, items):
        # playerdeclref? "sacrifice" declarationorreference
        if len(items) == 2:
            return SacrificeExpression(subject=items[1], controller=items[0])
        return SacrificeExpression(subject=items[0])

    def exileexpression(self, items):
        return ExileExpression(subject=items[0])

    def createexpression(self, items):
        # playerdeclref? "create"["s"] declarationorreference
        return CreateTokenExpression(descriptor=items[-1])

    def returnexpression(self, items):
        # playerdeclref? "return"["s"] declarationorreference atrandomexpression?
        # ("from" zonedeclarationexpression)? "to" zonedeclarationexpression
        # genericdeclarationexpression? zoneplacementmodifier?
        # We grab subject, optional origin, and destination using Name markers.
        nonnull = [it for it in items if it is not None]
        if len(nonnull) < 2:
            raise LoweringIncomplete("return-too-few-children")
        return ReturnExpression(subject=nonnull[0], destination=nonnull[-1])

    def revealexpression(self, items):
        return RevealExpression()

    def lookexpression(self, items):
        # playerdeclref? ("look"["s"]|"looked") "at"
        #   (declarationorreference | cardexpression | zonedeclarationexpression)
        return LookExpression()

    def surveilexpression(self, items):
        # "surveil" valueexpression
        return SurveilExpression(caliber=items[0])

    def conniveexpression(self, items):
        # declarationorreference? "connive"["s"] valueexpression?
        # Surface-only stub mirroring gainlifeexpression: subject and amount
        # are dropped until a card needs them.
        return ConniveExpression(subject=None)

    def gainlifeexpression(self, items):
        # playerdeclref? "gain"["s"] (valueexpression? "life" | "life" valueexpression)
        # | playerdeclref "gained" (valueexpression? "life" | "life" valueexpression) timeexpression?
        # Surface-only stub matching Reed's GainLoseExpression shape; amount
        # and player are dropped until a card needs them.
        return GainLoseExpression(subject=None)

    def gainabilityexpression(self, items):
        # declarationorreference? "gain"["s"] abilitysequencestatement
        # Mirrors gainlifeexpression's surface-only stub.
        return GainLoseExpression(subject=None)

    def chooseexpression(self, items):
        # playerdeclref? ("choose"["s"]|"chose") declarationorreference
        #   ("other" "than" declarationorreference)? ("from" "it")? atrandomexpression?
        # Stub: surface the first child as the operand; "other than" and the
        # at-random / from-it modifiers are dropped until a card needs them.
        operand = items[0] if items else Name(name="")
        return ChoiceExpression(operand=operand)

    def controlsexpression(self, items):
        # playerdeclref? ("control"["s"] | "controlled") genericdeclarationexpression
        # Surface-only stub mirroring controlpostfix: keep the controller; the
        # controlled declaration is dropped until a card needs it.
        controller = items[0] if len(items) == 2 else NameReference(antecedent=Name(name="you"))
        return ControlExpression(controller=controller)

    def countertype(self, items):
        # `countertype: ptchangeexpression | WORD`. Pass the matched child
        # through — its surface form (PTExpression or Name) is what consumers
        # like putcounterexpression want.
        token = items[0]
        return Name(name=str(token)) if isinstance(token, Token) else token

    def putcounterexpression(self, items):
        # `playerdeclref? "put"["s"] ("a"|valueexpression) countertype "counter"["s"] "on" <ref>`
        # Day-one stub: collapse to AddRemoveExpression carrying the target.
        subject = items[-1] if items else None
        return AddRemoveExpression(subject=subject)

    def movecounterexpression(self, items):
        # `playerdeclref? "move"["s"] ("a"|valueexpression) countertype "counter"["s"]
        #  "from" declarationorreference ("onto"|"to") declarationorreference`
        # Day-one stub mirroring putcounterexpression: collapse to
        # AddRemoveExpression carrying the destination.
        subject = items[-1] if items else None
        return AddRemoveExpression(subject=subject)

    def preventdamagevariante(self, items):
        # "prevent that <DAMAGETYPE>" / "prevent all <DAMAGETYPE> that? <ref>? would deal ..."
        # Surface-only stub: carry the matched children so future lowering can read them.
        descriptors: list[Expression] = []
        for it in items:
            if isinstance(it, Token):
                descriptors.append(Name(name=str(it)))
            else:
                descriptors.append(it)
        return PreventDamageExpression(descriptors=tuple(descriptors))

    def redirectalldamageexpression(self, items):
        # "all" DAMAGETYPE "that" "would" "be" "dealt" "to" declref "is" "dealt" "to" declref
        # Surface-only stub mirroring preventdamagevariante.
        descriptors: list[Expression] = []
        for it in items:
            if isinstance(it, Token):
                descriptors.append(Name(name=str(it)))
            else:
                descriptors.append(it)
        return RedirectAllDamageExpression(descriptors=tuple(descriptors))

    def uncastexpression(self, items):
        return UncastExpression(subject=items[0])

    def castexpression(self, items):
        # playerdeclref? "next"? "cast"["s"] declarationorreference
        #   (castmodifier ("and" castmodifier)?)* timeexpression?
        # Surface-only stub mirroring chooseexpression: surface the first
        # child as the subject; playerdeclref, castmodifiers, and
        # timeexpression are dropped until a card needs them.
        subject = items[0] if items else Name(name="")
        return CastExpression(subject=subject)

    def copyexpression(self, items):
        # playerdeclref? ("copy" | "copies" | "copied") declarationorreference
        # Surface-only stub mirroring castexpression: surface the
        # declarationorreference as the subject; playerdeclref is dropped
        # until a card needs it.
        subject = items[-1] if items else Name(name="")
        return CopyExpression(subject=subject)

    def drawexpression(self, items):
        # playerdeclref? ("draw"["s"]|"drew") cardexpression
        # Reed's transformer attached the player to the surface; we don't
        # currently carry a player field on CardDrawExpression, so drop it.
        card_expr = items[-1]
        quantity = _quantity_from_cardexpression(card_expr)
        return CardDrawExpression(quantity=quantity)

    def cardexpression(self, items):
        # Carries through raw - drawexpression extracts the quantity.
        return items

    def attacksexpression(self, items):
        # "attacks" — we return a minimal Name marker. Triggered-ability
        # promotion in regularability looks for this shape.
        return Name(name="attacks")

    def attackedexpression(self, items):
        return Name(name="attacked")

    def blocksexpression(self, items):
        return Name(name="blocks")

    def blockedexpression(self, items):
        return Name(name="blocked")

    def enterzoneexpression(self, items):
        subject = items[0]
        zone = items[1] if len(items) > 1 else Name(name="the battlefield")
        return ChangeZoneExpression(subject=subject, zone=zone, entering=True)

    def leavezoneexpression(self, items):
        subject = items[0]
        zone = items[1] if len(items) > 1 else Name(name="the battlefield")
        return ChangeZoneExpression(subject=subject, zone=zone, entering=False)

    def tapexpression(self, items):
        subject = items[0] if items else Name(name="")
        return TapUntapExpression(subject=subject, tap=True, untap=False)

    def untapexpression(self, items):
        subject = items[0] if items else Name(name="")
        return TapUntapExpression(subject=subject, tap=False, untap=True)

    def getsptexpression(self, items):
        # declarationorreference? "gets" ptchangeexpression
        # The first item is the subject; the second is the PT change.
        # We expose this as an ExpressionStatement carrying a synthetic
        # DescriptionExpression so the BLB stat-mod path round-trips.
        subject = items[0] if len(items) > 1 else None
        pt = items[-1]
        descriptors: list[Expression] = []
        if subject is not None:
            descriptors.append(subject)
        descriptors.append(pt)
        return DescriptionExpression(descriptors=tuple(descriptors))

    def searchexpression(self, items):
        # playerdeclref? ("search"["es"] | "searched") zonedeclarationexpression?
        # "for" declarationorreference
        owner = items[0] if len(items) >= 2 else NameReference(antecedent=Name(name="you"))
        subject = items[-1]
        return SearchLibraryExpression(owner=owner, subject=subject)

    def shuffleexpression(self, items):
        owner = items[0] if items else NameReference(antecedent=Name(name="you"))
        return ShuffleLibraryExpression(owner=owner)

    def targetsexpression(self, items):
        # objectdeclref? "target"["s"] declarationorreference
        # Used as a subject in compounduntilstatement bodies. We return
        # the operand wrapped in TargetExpression so the surface text
        # matches "target creature".
        if not items:
            return TargetExpression(operand=None, is_any=False)
        return TargetExpression(operand=items[-1], is_any=False)

    # -- Mana --------------------------------------------------------------

    def manasymbolexpression(self, items):
        return ManaExpression(symbols=tuple(items))

    def puremanaexpression(self, items):
        return items[0]

    def manadescriptionexpression(self, items):
        return items[0]

    def manadefinition(self, items):
        return items[0]

    def manasymbol(self, items):
        return items[0]

    def manamarkerseq(self, items):
        return items[0]

    def regularmanasymbol(self, items):
        return Name(name=str(items[0]))

    def genericmanasymbol(self, items):
        return Name(name=str(items[0]))

    def whitemarker(self, items):
        return Name(name="W")

    def bluemarker(self, items):
        return Name(name="U")

    def blackmarker(self, items):
        return Name(name="B")

    def redmarker(self, items):
        return Name(name="R")

    def greenmarker(self, items):
        return Name(name="G")

    def xmarker(self, items):
        return Name(name="X")

    def xmanasymbol(self, items):
        return items[0]

    def colorlessmarker(self, items):
        return Name(name="C")

    def colorlessmanasymbol(self, items):
        return items[0]

    def snowmarker(self, items):
        return Name(name="S")

    def snowmanasymbol(self, items):
        return items[0]

    def phyrexianmarker(self, items):
        return Name(name="P")

    def phyrexianmanasymbol(self, items):
        return Name(name=f"{items[0].name}/P")

    def hybridmanasymbol(self, items):
        return Name(name=f"{items[0].name}/{items[1].name}")

    def alternate2manasymbol(self, items):
        return Name(name=f"2/{items[0].name}")

    def halfmarker(self, items):
        return Name(name="H")

    def halfmanasymbol(self, items):
        return Name(name=f"H{items[1].name}")

    # -- Tap/untap symbol --------------------------------------------------

    def tapuntapsymbol(self, items):
        token = items[0]
        return Name(name=str(token))

    # -- Catch-all ---------------------------------------------------------

    def __default__(self, data, children, meta):
        """Any unmodeled rule raises a labelled :class:`LoweringIncomplete`.

        ``data`` is the rule name. ``children`` is the list of already-
        transformed children. Surfacing the rule name preserves the
        "tell me exactly which shape is missing" property we want for the
        BLB-day-one corpus.
        """
        # Skip a few rules that are pure pass-throughs and benign to surface.
        raise LoweringIncomplete(f"unmodeled-rule:{data}")


# ---------------------------------------------------------------------------
# Helpers operating on the produced AST
# ---------------------------------------------------------------------------


def _signed_number(sign: str, value: NumberValue) -> NumberValue:
    """Apply a +/- sign to a NumberValue. Surface text is preserved."""
    if sign == "+":
        return value
    if sign == "-" and isinstance(value.value, int):
        return NumberValue(value=-value.value, ntype=value.ntype)
    return value


def _damage_type_for(text: str) -> DamageTypeEnum:
    t = text.lower()
    if "combat" in t and "noncombat" not in t:
        return DamageTypeEnum.COMBAT
    if "noncombat" in t:
        return DamageTypeEnum.NONCOMBAT
    return DamageTypeEnum.REGULAR


def _quantity_from_cardexpression(card_expr: Any) -> Expression:
    """Pull the quantity (NumberValue) out of a raw cardexpression child list.

    Reed's cardexpression rule is ``!cardexpression``-bang, so children are
    raw tokens/trees. We look for the first NumberValue or, failing that,
    treat an ``"a"`` token as a singular indefinite.
    """
    if not isinstance(card_expr, list):
        return NumberValue(value=1, ntype=NumberTypeEnum.LITERAL)
    for it in card_expr:
        if isinstance(it, NumberValue):
            return it
        if isinstance(it, Token) and str(it).lower() == "a":
            return NumberValue(value=1, ntype=NumberTypeEnum.LITERAL)
    return NumberValue(value=1, ntype=NumberTypeEnum.LITERAL)


def _wrap_modifiers(defn: Expression, modifiers: list[Expression]) -> Expression:
    """Apply modifier wrappers (target, each, etc.) to a definition.

    We compose right-to-left so the outermost modifier in source order is
    the outermost wrapper.
    """
    out: Expression = defn
    for mod in reversed(modifiers):
        if isinstance(mod, TargetExpression):
            out = TargetExpression(operand=out, is_any=mod.is_any)
        elif isinstance(mod, EachExpression):
            out = EachExpression(operand=out)
        elif isinstance(mod, IndefiniteSingularExpression):
            out = IndefiniteSingularExpression(operand=out)
        # Other markers (indefinite article, definite article, etc.) are
        # surface-only and dropped on day one.
    return out


def _attach_reminder(ability: KeywordAbility, reminder: ReminderText) -> KeywordAbility:
    """Return a copy of ``ability`` with ``reminder_text`` set.

    Frozen dataclasses use ``dataclasses.replace`` semantics; we do that
    manually since some classes share the field but not the type.
    """
    try:
        from dataclasses import replace as _replace

        return _replace(ability, reminder_text=reminder)
    except (TypeError, ValueError):
        return ability


def _try_promote_triggered(
    block: StatementBlock,
) -> tuple[Statement, Statement] | None:
    """If ``block`` is a single conditional, surface it as a TriggeredAbility.

    Returns ``(outcome, condition)`` if promotion applies, else ``None``.
    The caller assembles the actual :class:`TriggeredAbility`.
    """
    if len(block.statements) != 1:
        return None
    stmt = block.statements[0]
    if isinstance(stmt, (WhenStatement, WheneverStatement, AtStatement)):
        # Surface the condition as the trigger and the consequence as the body.
        # Returning (outcome, condition) per the dataclass field order.
        cond = stmt.conditional if isinstance(stmt.conditional, Statement) else _wrap_as_statement(stmt.conditional)
        return stmt.consequence, cond
    return None


def _wrap_as_statement(expr: Any) -> Statement:
    if isinstance(expr, Statement):
        return expr
    if isinstance(expr, Expression):
        return ExpressionStatement(root=expr)
    return ExpressionStatement(root=Name(name=str(expr)))


def _compound_pair(a: Statement, b: Statement) -> Statement:
    """Compose two statements with the AND terminator."""
    return CompoundStatement(statements=(a, b), terminator=CompoundTerminator.AND)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


_CONTRACTION_REPLACEMENTS = (
    ("his or her", "their"),
    ("it's", "it is"),
    ("you're", "you are"),
    ("they're", "they are"),
    ("you've", "you have"),
    ("isn't", "is not"),
    ("aren't", "are not"),
    ("don't", "do not"),
    ("doesn't", "does not"),
    ("can't", "can not"),
    ("that's", "that is"),
    ("each get", "get"),
    ("each gain", "gain"),
    ("each lose", "lose"),
    ("each draw", "draw"),
    ("each discard", "discard"),
    ("each sacrifice", "sacrifice"),
)


def _preprocess(text: str, name: str | None) -> str:
    """Reed's prelex step, inlined here so we own the order of operations.

    The shipping :class:`~argentum_press.parser.grammar.preprocessor.MtgJsonPreprocessor`
    has a parameter-order bug (``prelex(self, inputobj, flags, name)``)
    that we don't want to fix in this PR. Rewriting the substitution
    inline is cheap and lets us own the contract.
    """
    if name:
        # Alchemy variant cards are named "A-Foo" but their oracle text refers
        # to the base name "Foo" (e.g. A-Heartfire Hero says "Heartfire Hero
        # deals damage..."). Try the base name first when the A- prefix is
        # present so the substitution actually fires.
        candidates: list[str] = []
        if name.startswith("A-"):
            candidates.append(name[2:])
        candidates.append(name)
        for candidate in candidates:
            if "," in candidate:
                head = candidate.split(",", 1)[0]
                text = _ci_replace(text, candidate, "~f")
                text = _ci_replace(text, head, "~")
            else:
                text = _ci_replace(text, candidate, "~")
    for old, new in _CONTRACTION_REPLACEMENTS:
        text = _ci_replace(text, old, new)
    # Quoted-period sentinel: ".\"" -> ".\"."
    text = text.replace('."', '."."')
    return text


def _ci_replace(text: str, old: str, new: str) -> str:
    if not old:
        return text
    return re.sub(re.escape(old), new, text, flags=re.IGNORECASE)


# Lazily cached parser so we don't recompile the 939-line grammar per call.
_PARSER: lark.Lark | None = None


def _get_parser() -> lark.Lark:
    global _PARSER
    if _PARSER is None:
        # Local import to avoid a hard module-load cost on bare AST users.
        from argentum_press.parser.grammar.compiler import MtgJsonCompiler

        _PARSER = MtgJsonCompiler().getParser()
    return _PARSER


def transform(tree: Tree) -> Card:
    """Lower a parsed Lark ``cardtext`` tree to a :class:`Card`.

    Raises :class:`LoweringIncomplete` if any unmodeled shape is hit. Lark
    wraps exceptions thrown by *named* transformer methods (anything other
    than ``__default__``) in :class:`lark.exceptions.VisitError` via
    ``_call_userfunc``; we unwrap here so ``parse()``'s single
    ``except LoweringIncomplete`` catches both code paths the same way.
    Without this an explicit ``raise LoweringIncomplete(...)`` from e.g.
    ``abilityword`` propagates uncaught and crashes the gap-finding worker.
    """
    if tree is None:
        return Card(text_box=TextBox(lines=()), abilities=())
    from lark.exceptions import VisitError
    try:
        result = CardTransformer().transform(tree)
    except VisitError as e:
        if isinstance(e.orig_exc, LoweringIncomplete):
            raise e.orig_exc from e
        raise
    if not isinstance(result, Card):
        # Defensive: the cardtext method always returns Card, but if Lark
        # returns the inner ability list directly (e.g. start-rule oddity)
        # we coerce it.
        if isinstance(result, list):
            return Card(text_box=TextBox(lines=tuple(result)), abilities=tuple(result))
        raise LoweringIncomplete(f"unexpected-root-result:{type(result).__name__}")
    return result


def _lark_error_details(
    exc: UnexpectedInput, preprocessed: str, raw_message: str
) -> ParseErrorDetails:
    # Lark's UnexpectedInput hierarchy has three concrete subclasses with
    # slightly different attrs:
    #   UnexpectedToken       .token / .expected
    #   UnexpectedCharacters  .char  / .allowed
    #   UnexpectedEOF         .expected (no .token; we synthesise <EOF>)
    # All three expose .line / .column / .pos_in_stream via the base.
    token = getattr(exc, "token", None)
    char = getattr(exc, "char", None)
    if token is not None:
        unexpected = f"{token!s} (type={token.type!s})" if hasattr(token, "type") else str(token)
    elif char is not None:
        unexpected = repr(char)
    else:
        unexpected = "<EOF>"

    expected_set: set[str] = set()
    for attr in ("expected", "allowed", "accepts"):
        v = getattr(exc, attr, None)
        if v:
            expected_set.update(str(x) for x in v)
    # Earley's UnexpectedEOF leaves .expected as []. The full token list is
    # only in str(exc), formatted as "\t* TOKEN" after "Expected one of:".
    # Fall back to scraping it so the orchestrator has something to show.
    if not expected_set and "Expected one of" in raw_message:
        for line in raw_message.splitlines():
            stripped = line.lstrip().lstrip("*").strip()
            if stripped and line.lstrip().startswith("*"):
                expected_set.add(stripped)

    try:
        context = exc.get_context(preprocessed)
    except Exception:
        context = ""

    return ParseErrorDetails(
        preprocessed_text=preprocessed,
        line=int(getattr(exc, "line", 0) or 0),
        column=int(getattr(exc, "column", 0) or 0),
        pos_in_stream=int(getattr(exc, "pos_in_stream", 0) or 0),
        unexpected=unexpected,
        expected=tuple(sorted(expected_set)),
        context=context,
        raw_message=raw_message,
    )


def parse(card: dict | str, *, name: str | None = None) -> ParseResult:
    """Full pipeline: Scryfall dict or raw oracle text -> :class:`ParseResult`.

    ``card`` may be:
      * A string of oracle text (the card's name should be passed via
        ``name=`` so ``~`` substitution works).
      * A Scryfall-shaped dict carrying ``name`` and ``oracle_text``.
    """
    if isinstance(card, dict):
        text = card.get("oracle_text", "") or ""
        card_name = card.get("name") if name is None else name
    else:
        text = card
        card_name = name

    if not text.strip():
        return ParseResult(ast=Card())

    preprocessed = _preprocess(text, card_name)
    parser = _get_parser()
    try:
        tree = parser.parse(preprocessed)
    except UnexpectedInput as e:
        raw = str(e)
        details = _lark_error_details(e, preprocessed, raw)
        # Build a discriminating label from the rich error details. The
        # raw first line is the generic "Unexpected X. Expected one of:";
        # the discriminating token list lives on subsequent lines and was
        # being truncated by .splitlines()[0], collapsing distinct parse
        # failures to the same label. We use ``unexpected`` + a locator:
        # pos_in_stream when Lark gives us one, or a hash of the expected
        # token set when it doesn't (UnexpectedEOF leaves position at -1
        # but exposes .expected, and two EOF failures expecting different
        # continuations need different fixes, so the expected-set is the
        # natural fingerprint). ``unexpected`` is normalized to "<EOF>"
        # when the upstream detail formatter produces the empty
        # " (type=<EOF>)" shape.
        unexpected_short = details.unexpected.strip()
        if not unexpected_short or "(type=<EOF>)" in unexpected_short:
            unexpected_short = "<EOF>"
        if details.pos_in_stream >= 0:
            locator = f"p{details.pos_in_stream}"
        else:
            # UnexpectedEOF under Earley + ambiguity often leaves
            # pos_in_stream=-1 and an empty .expected. Fall back to a hash
            # of the preprocessed text so the same card on a re-run gets
            # the same label (no-progress detection works) while distinct
            # cards get distinct labels (false-positive aborts don't).
            import hashlib
            text_sig = hashlib.sha256(
                details.preprocessed_text.encode("utf-8")
            ).hexdigest()[:8]
            locator = f"t{text_sig}"
        return ParseResult(error=ParseError(
            kind="incomplete",
            message=f"parse-error:{unexpected_short}@{locator}",
            details=details,
        ))
    except LarkError as e:
        return ParseResult(error=ParseError(kind="invalid", message=f"lark-error:{e!s}".splitlines()[0]))

    try:
        ast = transform(tree)
    except LoweringIncomplete as e:
        return ParseResult(error=ParseError(kind="incomplete", message=str(e)))
    return ParseResult(ast=ast)


__all__ = [
    "CardTransformer",
    "LoweringIncomplete",
    "ParseError",
    "ParseErrorDetails",
    "ParseResult",
    "parse",
    "transform",
]
