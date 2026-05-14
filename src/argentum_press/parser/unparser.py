# pyright: basic
"""Unparser for the argentum-press AST.

Inverts ``argentum_press.parser.parse(...)`` by emitting Magic oracle text
from a frozen-dataclass AST. Public entry point is :func:`unparse`.

This module is a direct port of Reed Milewicz's ``unparseToString`` methods
from mtgcompiler/frontend/AST/, adapted to the renamed dataclass surface in
``argentum_press.parser.ast``. The Mg-prefix has been dropped, leading
underscores on fields have been dropped, and the ~60 pure-marker keyword
abilities have been collapsed into ``SimpleKeywordAbility(keyword=...)``.

Strategy:

* :func:`unparse` is a ``functools.singledispatch`` entry; one ``@register``
  per concrete dataclass.
* Compound nodes (cards, statement blocks, etc.) delegate to ``unparse`` on
  their children rather than calling specific helpers, so the dispatch table
  stays flat and reorderings/insertions are local edits.
* Pure functions, no mutable state.
"""
from __future__ import annotations

from functools import singledispatch
from typing import Any

from argentum_press.parser.ast.abilities import (
    AbilityWord,
    AbsorbAbility,
    ActivatedAbility,
    AffinityAbility,
    AfflictAbility,
    AmplifyAbility,
    AnnihilatorAbility,
    AuraSwapAbility,
    AwakenAbility,
    BandingAbility,
    BestowAbility,
    BloodthirstAbility,
    BushidoAbility,
    BuybackAbility,
    ChampionAbility,
    CrewAbility,
    CumulativeUpkeepAbility,
    CyclingAbility,
    DashAbility,
    DevourAbility,
    DredgeAbility,
    EchoAbility,
    EmbalmAbility,
    EmergeAbility,
    EnchantAbility,
    EntwineAbility,
    EquipAbility,
    EscalateAbility,
    EternalizeAbility,
    EvokeAbility,
    FabricateAbility,
    FadingAbility,
    FlashbackAbility,
    ForecastAbility,
    FortifyAbility,
    FrenzyAbility,
    GraftAbility,
    HexproofAbility,
    HiddenAgendaAbility,
    JumpStartAbility,
    KickerAbility,
    LandwalkAbility,
    LevelUpAbility,
    MadnessAbility,
    MiracleAbility,
    ModularAbility,
    MorphAbility,
    NinjutsuAbility,
    OfferingAbility,
    OutlastAbility,
    OverloadAbility,
    PartnerAbility,
    PoisonousAbility,
    ProtectionAbility,
    ProwlAbility,
    RampageAbility,
    RecoverAbility,
    RegularAbility,
    ReinforceAbility,
    ReminderText,
    RenownAbility,
    ReplicateAbility,
    RippleAbility,
    ScavengeAbility,
    SimpleKeywordAbility,
    SoulshiftAbility,
    SpellAbility,
    SpliceAbility,
    StatementSequence,
    StaticAbility,
    SurgeAbility,
    SurveilAbility,
    SuspendAbility,
    TransfigureAbility,
    TransmuteAbility,
    TributeAbility,
    TriggeredAbility,
    UnearthAbility,
    VanishingAbility,
    WardAbility,
)
from argentum_press.parser.ast.card import (
    Card,
    FlavorText,
    TextBox,
    TypeLine,
)
from argentum_press.parser.ast.card_types import (
    CardType,
    Subtype,
    Supertype,
)
from argentum_press.parser.ast.colormana import (
    ColorTerm,
    ColorTermEnum,
    ManaModifier,
    ManaSymbol,
    ManaType,
)
from argentum_press.parser.ast.expressions import (
    AddManaExpression,
    AllExpression,
    AndExpression,
    AndOrExpression,
    AnyColorSpecifier,
    CardDrawExpression,
    ChangeZoneExpression,
    ChoiceExpression,
    ColorExpression,
    ControlExpression,
    CostSequenceExpression,
    CreateTokenExpression,
    DashCostExpression,
    DealsDamageExpression,
    DealsDamageVariant,
    DescriptionExpression,
    DestroyExpression,
    EachExpression,
    ExileExpression,
    GenericDeclarationExpression,
    IndefiniteSingularExpression,
    ManaExpression,
    ManaSpecificationExpression,
    ModalExpression,
    NamedExpression,
    NonExpression,
    NumberOfExpression,
    NumberTypeEnum,
    NumberValue,
    OrExpression,
    PossessiveExpression,
    PTExpression,
    PutInZoneExpression,
    ReturnExpression,
    SacrificeExpression,
    SearchLibraryExpression,
    ShuffleLibraryExpression,
    TapUntapExpression,
    TargetExpression,
    TypeExpression,
    UncastExpression,
    ValueEqExpression,
    ValueGtEqExpression,
    ValueGtExpression,
    ValueLtEqExpression,
    ValueLtExpression,
    WithExpression,
)
from argentum_press.parser.ast.keywords import Keyword
from argentum_press.parser.ast.references import (
    AbilityModifier,
    AbilityReference,
    CharacteristicTerm,
    CombatStatusModifier,
    DamageType,
    EffectStatusModifier,
    ItReference,
    KeywordStatusModifier,
    Name,
    NameReference,
    PhaseTerm,
    PLAYER_TERM_FORMS,
    PlayerTerm,
    Qualifier,
    SelfReference,
    StepTerm,
    TapStatusModifier,
    TapUntapSymbol,
    ThatReference,
    ThisReference,
    TurnTerm,
    Zone,
)
from argentum_press.parser.ast.statements import (
    AbilitySequenceStatement,
    ActivationStatement,
    AsLongAsStatement,
    AtStatement,
    BeingStatement,
    CompoundStatement,
    CompoundTerminator,
    ConditionalStatement,
    DuringStatement,
    ExpressionStatement,
    ForStatement,
    IfStatement,
    IsStatement,
    KeywordAbilityListStatement,
    MayStatement,
    OtherwiseStatement,
    QuotedAbilityStatement,
    StatementBlock,
    ThenStatement,
    UnlessStatement,
    UntilStatement,
    WhenStatement,
    WheneverStatement,
    WhileStatement,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@singledispatch
def unparse(node: Any) -> str:
    """Emit oracle text for an AST node.

    Raises:
        NotImplementedError: If ``node``'s concrete type has no registered
            handler. The unparser is intended to cover the closed hierarchy
            exported by ``argentum_press.parser.ast``; an unhandled type
            usually signals a new dataclass that needs a ``@unparse.register``
            entry here, not a parser bug.
    """
    raise NotImplementedError(
        f"unparse not implemented for {type(node).__name__}"
    )


# Convenience: pre-format an optional child or return "" if it is missing.
def _opt(node: Any) -> str:
    return "" if node is None else unparse(node)


# ---------------------------------------------------------------------------
# Card-level containers
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_card(node: Card) -> str:
    """Render a Card. We follow Reed's loose top-to-bottom ordering.

    If ``text_box`` is set, prefer it; otherwise stitch ``abilities``.
    """
    parts: list[str] = []
    if node.name is not None:
        parts.append(unparse(node.name))
    if node.mana_cost is not None:
        parts.append(unparse(node.mana_cost))
    if node.type_line is not None:
        parts.append(unparse(node.type_line))
    if node.text_box is not None:
        parts.append(unparse(node.text_box))
    elif node.abilities:
        parts.append("\n".join(unparse(a) for a in node.abilities))
    if node.power_toughness is not None:
        parts.append(unparse(node.power_toughness))
    if node.flavor is not None:
        parts.append(unparse(node.flavor))
    return "\n".join(p for p in parts if p)


@unparse.register
def _unparse_text_box(node: TextBox) -> str:
    return "\n".join(unparse(line) for line in node.lines)


@unparse.register
def _unparse_type_line(node: TypeLine) -> str:
    supertypes = _opt(node.supertypes)
    types = _opt(node.types)
    subtypes = _opt(node.subtypes)
    left = " ".join(p for p in (supertypes, types) if p)
    if subtypes:
        return f"{left} — {subtypes}".strip()
    return left


@unparse.register
def _unparse_flavor_text(node: FlavorText) -> str:
    return node.text


# ---------------------------------------------------------------------------
# Types / subtypes / supertypes
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_card_type(node: CardType) -> str:
    return node.value if isinstance(node.value, str) else node.value.value


@unparse.register
def _unparse_subtype(node: Subtype) -> str:
    return node.value if isinstance(node.value, str) else node.value.value


@unparse.register
def _unparse_supertype(node: Supertype) -> str:
    return node.value if isinstance(node.value, str) else node.value.value


# ---------------------------------------------------------------------------
# Mana symbols and color terms
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_mana_symbol(node: ManaSymbol) -> str:
    """Render a mana symbol such as ``{G}``, ``{U/W}``, ``{2}``, ``{G/P}``.

    Note: this is a best-effort renderer for symbols with no in-game
    equivalent (e.g. half-blue-phyrexian); follows Reed verbatim.
    """
    output = ""
    if node.color is not None:
        sequence: list[str] = []
        if ManaType.WHITE in node.color:
            sequence.append("W")
        if ManaType.BLUE in node.color:
            sequence.append("U")
        if ManaType.BLACK in node.color:
            sequence.append("B")
        if ManaType.RED in node.color:
            sequence.append("R")
        if ManaType.GREEN in node.color:
            sequence.append("G")
        if ManaType.COLORLESS in node.color:
            sequence.append("C")
        output += "/".join(sequence)
    elif node.modifiers is None:
        # Generic mana (X, *, integer)
        output += str(node.cvalue)
    if node.modifiers is not None:
        if ManaModifier.PHYREXIAN in node.modifiers:
            output += "/P"
        if ManaModifier.ALTERNATE_TWO in node.modifiers:
            output += "/2"
        if ManaModifier.HALF in node.modifiers:
            output = "H" + output
        if ManaModifier.SNOW in node.modifiers:
            output = "S" + output
    return "{" + output + "}"


@unparse.register
def _unparse_color_term(node: ColorTerm) -> str:
    if isinstance(node.value, str):
        return node.value
    return node.value.value


# ---------------------------------------------------------------------------
# References / modifiers / terms (leaves)
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_name(node: Name) -> str:
    return node.name


@unparse.register
def _unparse_name_reference(node: NameReference) -> str:
    if node.antecedent is None:
        return "~f" if node.first_name_only else "~"
    rendered = unparse(node.antecedent)
    if node.first_name_only:
        # "Arashi, the Sky Asunder" -> "Arashi"
        return rendered.split(", ", 1)[0]
    return rendered


@unparse.register
def _unparse_self_reference(node: SelfReference) -> str:
    return node.reftype.value


@unparse.register
def _unparse_that_reference(node: ThatReference) -> str:
    return f"that {unparse(node.descriptor)}"


@unparse.register
def _unparse_this_reference(node: ThisReference) -> str:
    return f"this {unparse(node.descriptor)}"


@unparse.register
def _unparse_it_reference(_node: ItReference) -> str:
    return "it"


@unparse.register
def _unparse_ability_reference(_node: AbilityReference) -> str:
    # Reed's MgAbilityReference had no concrete unparseToString; emit "ability".
    return "ability"


@unparse.register
def _unparse_player_term(node: PlayerTerm) -> str:
    forms = PLAYER_TERM_FORMS[node.value]
    return forms["nominative_plural" if node.is_plural else "nominative_singular"]


@unparse.register
def _unparse_damage_type(node: DamageType) -> str:
    return node.value.value


@unparse.register
def _unparse_ability_modifier(node: AbilityModifier) -> str:
    return node.value.value


@unparse.register
def _unparse_combat_status_modifier(node: CombatStatusModifier) -> str:
    return node.value.value


@unparse.register
def _unparse_keyword_status_modifier(node: KeywordStatusModifier) -> str:
    return node.value.value


@unparse.register
def _unparse_tap_status_modifier(node: TapStatusModifier) -> str:
    return node.value.value


@unparse.register
def _unparse_effect_status_modifier(node: EffectStatusModifier) -> str:
    return node.value.value


@unparse.register
def _unparse_characteristic_term(node: CharacteristicTerm) -> str:
    return node.value.value


@unparse.register
def _unparse_qualifier(node: Qualifier) -> str:
    return node.value.value


@unparse.register
def _unparse_phase_term(node: PhaseTerm) -> str:
    return node.value.value


@unparse.register
def _unparse_step_term(node: StepTerm) -> str:
    return node.value.value


@unparse.register
def _unparse_turn_term(_node: TurnTerm) -> str:
    return "turn"


@unparse.register
def _unparse_zone(node: Zone) -> str:
    return node.value.value


@unparse.register
def _unparse_tap_untap_symbol(node: TapUntapSymbol) -> str:
    return "{T}" if node.is_tap else "{Q}"


# ---------------------------------------------------------------------------
# Value comparisons / numbers
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_value_gt(node: ValueGtExpression) -> str:
    if node.lhs is not None:
        return f"{unparse(node.lhs)} greater than {unparse(node.rhs)}"
    return f"greater than {unparse(node.rhs)}"


@unparse.register
def _unparse_value_gteq(node: ValueGtEqExpression) -> str:
    if node.short_variant:
        if node.lhs is not None:
            return f"{unparse(node.lhs)} {unparse(node.rhs)} or greater"
        return f"{unparse(node.rhs)} or greater"
    if node.lhs is not None:
        return f"{unparse(node.lhs)} greater than or equal to {unparse(node.rhs)}"
    return f"greater than or equal to {unparse(node.rhs)}"


@unparse.register
def _unparse_value_lt(node: ValueLtExpression) -> str:
    if node.lhs is not None:
        return f"{unparse(node.lhs)} less than {unparse(node.rhs)}"
    return f"less than {unparse(node.rhs)}"


@unparse.register
def _unparse_value_lteq(node: ValueLtEqExpression) -> str:
    if node.short_variant:
        if node.lhs is not None:
            return f"{unparse(node.lhs)} {unparse(node.rhs)} or less"
        return f"{unparse(node.rhs)} or less"
    if node.lhs is not None:
        return f"{unparse(node.lhs)} less than or equal to {unparse(node.rhs)}"
    return f"less than or equal to {unparse(node.rhs)}"


@unparse.register
def _unparse_value_eq(node: ValueEqExpression) -> str:
    if node.lhs is not None:
        return f"{unparse(node.lhs)} equal to {unparse(node.rhs)}"
    return f"equal to {unparse(node.rhs)}"


@unparse.register
def _unparse_number_value(node: NumberValue) -> str:
    """Render NumberValue. Magic English cardinals/ordinals/frequencies need
    ``num2words``; we keep that dependency optional and fall back to ``str``
    if the value is already a string or num2words is unavailable.
    """
    value = node.value
    ntype = node.ntype
    if ntype is NumberTypeEnum.CUSTOM:
        return str(value)
    if ntype is NumberTypeEnum.LITERAL:
        return str(value)
    if ntype is NumberTypeEnum.FREQUENCY and value == 1:
        return "once"
    if ntype is NumberTypeEnum.FREQUENCY and value == 2:
        return "twice"
    try:
        from num2words import num2words as _n2w  # type: ignore[import-untyped]
    except ImportError:
        # Best-effort fallback: just emit the integer.
        return str(value)
    if ntype is NumberTypeEnum.CARDINAL:
        return _n2w(value)
    if ntype is NumberTypeEnum.FREQUENCY:
        return f"{_n2w(value)} times"
    if ntype is NumberTypeEnum.ORDINAL:
        return _n2w(value, to="ordinal")
    return str(value)


@unparse.register
def _unparse_number_of(node: NumberOfExpression) -> str:
    return f"the number of {unparse(node.expression)}"


# ---------------------------------------------------------------------------
# Mana expressions / specifiers
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_mana_expression(node: ManaExpression) -> str:
    return "".join(unparse(sym) for sym in node.symbols)


@unparse.register
def _unparse_mana_specification(node: ManaSpecificationExpression) -> str:
    head = unparse(node.quantity)
    rest = " ".join(unparse(s) for s in node.specifiers)
    return f"{head} {rest}" if rest else head


@unparse.register
def _unparse_any_color_specifier(node: AnyColorSpecifier) -> str:
    return "mana of any one color" if node.any_one_color else "mana of any color"


# ---------------------------------------------------------------------------
# Cost expressions
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_cost_sequence(node: CostSequenceExpression) -> str:
    return ", ".join(unparse(arg) for arg in node.arguments)


@unparse.register
def _unparse_dash_cost(node: DashCostExpression) -> str:
    return f"—{unparse(node.cost)}"


# ---------------------------------------------------------------------------
# Declarations / descriptions
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_generic_declaration(node: GenericDeclarationExpression) -> str:
    return unparse(node.definition)


@unparse.register
def _unparse_description(node: DescriptionExpression) -> str:
    return " ".join(unparse(d) for d in node.descriptors)


# ---------------------------------------------------------------------------
# P/T, color, type
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_pt(node: PTExpression) -> str:
    return f"{unparse(node.power)}/{unparse(node.toughness)}"


@unparse.register
def _unparse_color_expression(node: ColorExpression) -> str:
    return unparse(node.value)


@unparse.register
def _unparse_type_expression(node: TypeExpression) -> str:
    if not node.types:
        return ""
    rendered = [unparse(t) for t in node.types]
    if node.comma_delimited and len(rendered) > 1:
        head = ", ".join(rendered[:-1])
        return f"{head} {rendered[-1]}"
    return " ".join(rendered)


# ---------------------------------------------------------------------------
# Control / possession / modal / change zone
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_control(node: ControlExpression) -> str:
    return f"{unparse(node.controller)} control(s)"


@unparse.register
def _unparse_possessive(node: PossessiveExpression) -> str:
    # Reed prefers PlayerTerm.possessive when the possessor is a PlayerTerm;
    # otherwise fall back to the "X's Y" form.
    possessor = node.possessor
    if isinstance(possessor, PlayerTerm):
        forms = PLAYER_TERM_FORMS[possessor.value]
        key = "possessive_plural" if possessor.is_plural else "possessive_singular"
        return f"{forms[key]} {unparse(node.owned)}"
    return f"{unparse(possessor)}'s {unparse(node.owned)}"


@unparse.register
def _unparse_modal(node: ModalExpression) -> str:
    head = f"Choose {unparse(node.number_of_choices)} —\n"
    options = "".join(f"• {unparse(opt)}\n" for opt in node.options)
    return head + options


@unparse.register
def _unparse_change_zone(node: ChangeZoneExpression) -> str:
    verb = "enters" if node.entering else "leaves"
    return f"{unparse(node.subject)} {verb} {unparse(node.zone)}"


# ---------------------------------------------------------------------------
# Binary / unary operators
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_and(node: AndExpression) -> str:
    return f"{unparse(node.lhs)} and {unparse(node.rhs)}"


@unparse.register
def _unparse_or(node: OrExpression) -> str:
    return f"{unparse(node.lhs)} or {unparse(node.rhs)}"


@unparse.register
def _unparse_andor(node: AndOrExpression) -> str:
    return f"{unparse(node.lhs)} and/or {unparse(node.rhs)}"


@unparse.register
def _unparse_target(node: TargetExpression) -> str:
    if node.is_any:
        return "any target"
    return f"target {unparse(node.operand)}" if node.operand is not None else "target"


@unparse.register
def _unparse_all(node: AllExpression) -> str:
    return f"all {unparse(node.operand)}"


@unparse.register
def _unparse_each(node: EachExpression) -> str:
    return f"each {unparse(node.operand)}"


@unparse.register
def _unparse_indef(node: IndefiniteSingularExpression) -> str:
    """``a`` / ``an``. Pick the article based on the operand's leading vowel,
    upgrading Reed's literal ``a(n)`` placeholder.
    """
    rendered = unparse(node.operand)
    article = "an" if rendered[:1].lower() in {"a", "e", "i", "o", "u"} else "a"
    return f"{article} {rendered}"


@unparse.register
def _unparse_choice(node: ChoiceExpression) -> str:
    return f"choose {unparse(node.operand)}"


@unparse.register
def _unparse_non(node: NonExpression) -> str:
    return f"non-{unparse(node.operand)}"


@unparse.register
def _unparse_with(node: WithExpression) -> str:
    return f"with {unparse(node.operand)}"


@unparse.register
def _unparse_named(node: NamedExpression) -> str:
    return f"named {unparse(node.operand)}"


# ---------------------------------------------------------------------------
# Effect expressions
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_add_mana(node: AddManaExpression) -> str:
    if node.player is not None:
        return f"{unparse(node.player)} adds {unparse(node.mana)}"
    return f"add {unparse(node.mana)}"


@unparse.register
def _unparse_deals_damage(node: DealsDamageExpression) -> str:
    head = f"{unparse(node.origin)} deal(s)"
    amount = node.damage_amount
    subject = node.subject
    dtype = unparse(node.damage_type)
    if node.variant is DealsDamageVariant.A:
        # <origin> deals <amount?> damage to <subject?>
        out = head
        if amount is not None:
            out += f" {unparse(amount)} {dtype}"
        else:
            out += f" {dtype}"
        if subject is not None:
            out += f" to {unparse(subject)}"
        return out
    if node.variant is DealsDamageVariant.B:
        # <origin> deals damage <amount> to <subject>
        return (
            f"{head} {dtype} {unparse(amount) if amount else ''} "
            f"to {unparse(subject) if subject else ''}"
        ).strip()
    # Variant C: <origin> deals damage to <subject> <amount>
    return (
        f"{head} {dtype} to {unparse(subject) if subject else ''} "
        f"{unparse(amount) if amount else ''}"
    ).strip()


@unparse.register
def _unparse_destroy(node: DestroyExpression) -> str:
    return f"destroy {unparse(node.subject)}"


@unparse.register
def _unparse_sacrifice(node: SacrificeExpression) -> str:
    if node.controller is None:
        return f"sacrifice {unparse(node.subject)}"
    return f"{unparse(node.controller)} sacrifices {unparse(node.subject)}"


@unparse.register
def _unparse_exile(node: ExileExpression) -> str:
    return f"exile {unparse(node.subject)}"


@unparse.register
def _unparse_tap_untap(node: TapUntapExpression) -> str:
    if node.tap and node.untap:
        return f"tap or untap {unparse(node.subject)}"
    if node.untap and not node.tap:
        return f"untap {unparse(node.subject)}"
    return f"tap {unparse(node.subject)}"


@unparse.register
def _unparse_return(node: ReturnExpression) -> str:
    if node.origin is not None:
        return (
            f"return {unparse(node.subject)} from {unparse(node.origin)} "
            f"to {unparse(node.destination)}"
        )
    return f"return {unparse(node.subject)} to {unparse(node.destination)}"


@unparse.register
def _unparse_uncast(node: UncastExpression) -> str:
    return f"counter {unparse(node.subject)}"


@unparse.register
def _unparse_create_token(node: CreateTokenExpression) -> str:
    if node.quantity is None:
        return f"create {unparse(node.descriptor)}"
    return f"create {unparse(node.quantity)} {unparse(node.descriptor)}"


@unparse.register
def _unparse_card_draw(node: CardDrawExpression) -> str:
    # Single-card draw is special-cased: "draw a card" if quantity is 1.
    qty = node.quantity
    if isinstance(qty, NumberValue) and qty.value == 1:
        return "draw a card"
    return f"draw {unparse(qty)} cards"


@unparse.register
def _unparse_search_library(node: SearchLibraryExpression) -> str:
    return f"search {unparse(node.owner)} library for {unparse(node.subject)}"


@unparse.register
def _unparse_shuffle_library(node: ShuffleLibraryExpression) -> str:
    return f"shuffle {unparse(node.owner)} library"


@unparse.register
def _unparse_put_in_zone(node: PutInZoneExpression) -> str:
    if node.conditions is not None:
        return (
            f"put {unparse(node.subject)} into {unparse(node.zone)} "
            f"{unparse(node.conditions)}"
        )
    return f"put {unparse(node.subject)} into {unparse(node.zone)}"


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_statement_block(node: StatementBlock) -> str:
    return ". ".join(unparse(s) for s in node.statements)


@unparse.register
def _unparse_compound(node: CompoundStatement) -> str:
    if not node.statements:
        return ""
    parts = [unparse(s) for s in node.statements]
    if len(parts) == 1:
        return parts[0]
    head = ", ".join(parts[:-1])
    term = (
        "then" if node.terminator is CompoundTerminator.THEN else "and"
    )
    return f"{head}, {term} {parts[-1]}"


@unparse.register
def _unparse_expression_statement(node: ExpressionStatement) -> str:
    return unparse(node.root)


@unparse.register
def _unparse_may(node: MayStatement) -> str:
    return f"{unparse(node.player)} may {unparse(node.statement)}"


@unparse.register
def _unparse_being(node: BeingStatement) -> str:
    if node.lhs is None:
        return unparse(node.rhs)
    return f"{unparse(node.lhs)} {unparse(node.rhs)}"


@unparse.register
def _unparse_is_statement(_node: IsStatement) -> str:
    # Reed left this stubbed; preserve "is" as a token placeholder.
    return "is"


@unparse.register
def _unparse_then(node: ThenStatement) -> str:
    return f"Then {unparse(node.body)}"


@unparse.register
def _unparse_keyword_ability_list(node: KeywordAbilityListStatement) -> str:
    if not node.abilities:
        return ""
    rendered = [unparse(a) for a in node.abilities]
    # If reminder text is attached, the original used "; "; we just use ", "
    # since reminder text travels with each ability.
    return ", ".join(rendered)


@unparse.register
def _unparse_ability_sequence(node: AbilitySequenceStatement) -> str:
    if not node.abilities:
        return ""
    rendered = [unparse(a) for a in node.abilities]
    if len(rendered) == 1:
        return rendered[0]
    head = ",".join(rendered[:-1])
    return f"{head} and {rendered[-1]}"


@unparse.register
def _unparse_quoted_ability(node: QuotedAbilityStatement) -> str:
    return f'"{unparse(node.block)}"'


@unparse.register
def _unparse_activation_statement(node: ActivationStatement) -> str:
    return f"{unparse(node.cost)}: {unparse(node.instructions)}"


# ---- Conditional statements ----


def _format_conditional(
    keyword: str,
    inverted: bool,
    conditional: Any,
    consequence: Any,
) -> str:
    cond = unparse(conditional)
    cons = unparse(consequence)
    if inverted:
        # "<consequence> if <condition>"
        return f"{cons} {keyword} {cond}"
    # "<keyword> <condition>, <consequence>"
    return f"{keyword} {cond}, {cons}"


@unparse.register
def _unparse_if(node: IfStatement) -> str:
    return _format_conditional("if", node.inverted, node.conditional, node.consequence)


@unparse.register
def _unparse_whenever(node: WheneverStatement) -> str:
    return _format_conditional(
        "whenever", node.inverted, node.conditional, node.consequence
    )


@unparse.register
def _unparse_when(node: WhenStatement) -> str:
    return _format_conditional(
        "when", node.inverted, node.conditional, node.consequence
    )


@unparse.register
def _unparse_at(node: AtStatement) -> str:
    return _format_conditional("at", node.inverted, node.conditional, node.consequence)


@unparse.register
def _unparse_aslongas(node: AsLongAsStatement) -> str:
    return _format_conditional(
        "as long as", node.inverted, node.conditional, node.consequence
    )


@unparse.register
def _unparse_until(node: UntilStatement) -> str:
    return _format_conditional(
        "until", node.inverted, node.conditional, node.consequence
    )


@unparse.register
def _unparse_otherwise(node: OtherwiseStatement) -> str:
    return f"otherwise {unparse(node.conditional)}, {unparse(node.consequence)}"


@unparse.register
def _unparse_during(node: DuringStatement) -> str:
    qualifier = "only during" if node.exclusive else "during"
    return f"{unparse(node.conditional)} {qualifier} {unparse(node.consequence)}"


@unparse.register
def _unparse_unless(node: UnlessStatement) -> str:
    return f"{unparse(node.consequence)} unless {unparse(node.conditional)}"


@unparse.register
def _unparse_for(node: ForStatement) -> str:
    # Reed left this stubbed; preserve the "for each" lead-in if we have one.
    if node.conditional is None:
        return "for"
    if node.consequence is None:
        return f"for {unparse(node.conditional)}"
    return f"for {unparse(node.conditional)}, {unparse(node.consequence)}"


@unparse.register
def _unparse_while(node: WhileStatement) -> str:
    return _format_conditional(
        "while", node.inverted, node.conditional, node.consequence
    )


# ---------------------------------------------------------------------------
# Reminder text / ability word / statement sequence
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_reminder_text(node: ReminderText) -> str:
    return f"({node.text})"


@unparse.register
def _unparse_ability_word(node: AbilityWord) -> str:
    return f"{node.word} —"


@unparse.register
def _unparse_statement_sequence(node: StatementSequence) -> str:
    return " ".join(unparse(s) for s in node.statements)


# ---------------------------------------------------------------------------
# Abilities — bodies (regular, spell, activated, triggered, static)
# ---------------------------------------------------------------------------


def _wrap_ability(body: str, ability_word: AbilityWord | None, reminder: ReminderText | None) -> str:
    parts: list[str] = []
    if ability_word is not None:
        parts.append(unparse(ability_word))
    parts.append(body)
    if reminder is not None:
        parts.append(unparse(reminder))
    return " ".join(p for p in parts if p)


@unparse.register
def _unparse_regular_ability(node: RegularAbility) -> str:
    return _wrap_ability(unparse(node.block), node.ability_word, node.reminder_text)


@unparse.register
def _unparse_spell_ability(node: SpellAbility) -> str:
    return _wrap_ability(
        unparse(node.instructions), node.ability_word, node.reminder_text
    )


@unparse.register
def _unparse_activated_ability(node: ActivatedAbility) -> str:
    body = f"{unparse(node.cost)}: {unparse(node.instructions)}"
    return _wrap_ability(body, node.ability_word, node.reminder_text)


@unparse.register
def _unparse_triggered_ability(node: TriggeredAbility) -> str:
    # The condition itself usually carries the trigger keyword (whenever/when/at).
    body = f"{unparse(node.condition)}, {unparse(node.outcome)}"
    return _wrap_ability(body, node.ability_word, node.reminder_text)


@unparse.register
def _unparse_static_ability(node: StaticAbility) -> str:
    body = "" if node.block is None else unparse(node.block)
    return _wrap_ability(body, node.ability_word, node.reminder_text)


# ---------------------------------------------------------------------------
# Keyword abilities — simple (table-driven) + parametric
# ---------------------------------------------------------------------------


# Keywords that historically have a hyphen on their text but use underscores
# in the enum name; the enum.value already carries the right spelling.
def _kw(keyword: Keyword) -> str:
    return keyword.value


@unparse.register
def _unparse_simple_keyword(node: SimpleKeywordAbility) -> str:
    body = _kw(node.keyword)
    if node.reminder_text is not None:
        return f"{body} {unparse(node.reminder_text)}"
    return body


def _kw_with_reminder(base: str, reminder: ReminderText | None) -> str:
    return f"{base} {unparse(reminder)}" if reminder is not None else base


@unparse.register
def _unparse_equip(node: EquipAbility) -> str:
    if node.quality is not None:
        body = f"equip {unparse(node.quality)} {unparse(node.cost)}"
    else:
        body = f"equip {unparse(node.cost)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_enchant(node: EnchantAbility) -> str:
    body = f"enchant {unparse(node.descriptor)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_hexproof(node: HexproofAbility) -> str:
    body = "hexproof" if node.quality is None else f"hexproof from {unparse(node.quality)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_landwalk(node: LandwalkAbility) -> str:
    body = "landwalk" if node.landtype is None else f"{unparse(node.landtype)}walk"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_protection(node: ProtectionAbility) -> str:
    quals = " and ".join(f"from {unparse(q)}" for q in node.qualities)
    body = f"protection {quals}" if quals else "protection"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_banding(node: BandingAbility) -> str:
    body = (
        "banding"
        if node.quality is None
        else f"bands with other {unparse(node.quality)}"
    )
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_rampage(node: RampageAbility) -> str:
    return _kw_with_reminder(f"rampage {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_cumulative_upkeep(node: CumulativeUpkeepAbility) -> str:
    return _kw_with_reminder(
        f"cumulative upkeep {unparse(node.cost)}", node.reminder_text
    )


@unparse.register
def _unparse_buyback(node: BuybackAbility) -> str:
    return _kw_with_reminder(f"buyback {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_cycling(node: CyclingAbility) -> str:
    base = (
        f"{unparse(node.cycling_type)}cycling"
        if node.cycling_type is not None
        else "cycling"
    )
    body = f"{base} {unparse(node.cost)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_echo(node: EchoAbility) -> str:
    return _kw_with_reminder(f"echo {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_fading(node: FadingAbility) -> str:
    return _kw_with_reminder(f"fading {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_kicker(node: KickerAbility) -> str:
    name = "multikicker" if node.is_multi else "kicker"
    body = f"{name} {unparse(node.cost)}" if node.cost is not None else name
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_flashback(node: FlashbackAbility) -> str:
    return _kw_with_reminder(f"flashback {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_madness(node: MadnessAbility) -> str:
    return _kw_with_reminder(f"madness {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_morph(node: MorphAbility) -> str:
    name = "megamorph" if node.is_mega else "morph"
    return _kw_with_reminder(f"{name} {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_amplify(node: AmplifyAbility) -> str:
    return _kw_with_reminder(f"amplify {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_affinity(node: AffinityAbility) -> str:
    return _kw_with_reminder(
        f"affinity for {unparse(node.descriptor)}", node.reminder_text
    )


@unparse.register
def _unparse_entwine(node: EntwineAbility) -> str:
    return _kw_with_reminder(f"entwine {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_modular(node: ModularAbility) -> str:
    return _kw_with_reminder(f"modular {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_bushido(node: BushidoAbility) -> str:
    return _kw_with_reminder(f"bushido {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_soulshift(node: SoulshiftAbility) -> str:
    return _kw_with_reminder(f"soulshift {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_splice(node: SpliceAbility) -> str:
    body = f"splice onto {unparse(node.splice_type)} {unparse(node.cost)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_offering(node: OfferingAbility) -> str:
    return _kw_with_reminder(
        f"{unparse(node.descriptor)} offering", node.reminder_text
    )


@unparse.register
def _unparse_ninjutsu(node: NinjutsuAbility) -> str:
    return _kw_with_reminder(f"ninjutsu {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_forecast(node: ForecastAbility) -> str:
    body = f"forecast — {unparse(node.activated_ability)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_dredge(node: DredgeAbility) -> str:
    return _kw_with_reminder(f"dredge {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_transmute(node: TransmuteAbility) -> str:
    return _kw_with_reminder(f"transmute {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_bloodthirst(node: BloodthirstAbility) -> str:
    return _kw_with_reminder(
        f"bloodthirst {unparse(node.caliber)}", node.reminder_text
    )


@unparse.register
def _unparse_replicate(node: ReplicateAbility) -> str:
    return _kw_with_reminder(f"replicate {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_graft(node: GraftAbility) -> str:
    return _kw_with_reminder(f"graft {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_recover(node: RecoverAbility) -> str:
    return _kw_with_reminder(f"recover {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_ripple(node: RippleAbility) -> str:
    return _kw_with_reminder(f"ripple {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_suspend(node: SuspendAbility) -> str:
    body = f"suspend {unparse(node.caliber)}—{unparse(node.cost)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_vanishing(node: VanishingAbility) -> str:
    body = (
        "vanishing"
        if node.caliber is None
        else f"vanishing {unparse(node.caliber)}"
    )
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_absorb(node: AbsorbAbility) -> str:
    return _kw_with_reminder(f"absorb {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_aura_swap(node: AuraSwapAbility) -> str:
    return _kw_with_reminder(f"aura swap {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_fortify(node: FortifyAbility) -> str:
    return _kw_with_reminder(f"fortify {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_frenzy(node: FrenzyAbility) -> str:
    return _kw_with_reminder(f"frenzy {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_poisonous(node: PoisonousAbility) -> str:
    return _kw_with_reminder(f"poisonous {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_transfigure(node: TransfigureAbility) -> str:
    return _kw_with_reminder(f"transfigure {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_champion(node: ChampionAbility) -> str:
    desc = unparse(node.descriptor)
    article = "an" if desc[:1].lower() in {"a", "e", "i", "o", "u"} else "a"
    body = f"champion {article} {desc}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_evoke(node: EvokeAbility) -> str:
    return _kw_with_reminder(f"evoke {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_prowl(node: ProwlAbility) -> str:
    return _kw_with_reminder(f"prowl {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_reinforce(node: ReinforceAbility) -> str:
    body = f"reinforce {unparse(node.caliber)}—{unparse(node.cost)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_devour(node: DevourAbility) -> str:
    return _kw_with_reminder(f"devour {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_unearth(node: UnearthAbility) -> str:
    return _kw_with_reminder(f"unearth {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_annihilator(node: AnnihilatorAbility) -> str:
    return _kw_with_reminder(
        f"annihilator {unparse(node.caliber)}", node.reminder_text
    )


@unparse.register
def _unparse_level_up(node: LevelUpAbility) -> str:
    return _kw_with_reminder(f"level up {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_miracle(node: MiracleAbility) -> str:
    return _kw_with_reminder(f"miracle {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_overload(node: OverloadAbility) -> str:
    return _kw_with_reminder(f"overload {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_scavenge(node: ScavengeAbility) -> str:
    return _kw_with_reminder(f"scavenge {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_bestow(node: BestowAbility) -> str:
    return _kw_with_reminder(f"bestow {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_tribute(node: TributeAbility) -> str:
    return _kw_with_reminder(f"tribute {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_hidden_agenda(node: HiddenAgendaAbility) -> str:
    body = "double agenda" if node.is_double_agenda else "hidden agenda"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_outlast(node: OutlastAbility) -> str:
    return _kw_with_reminder(f"outlast {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_dash(node: DashAbility) -> str:
    return _kw_with_reminder(f"dash {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_renown(node: RenownAbility) -> str:
    return _kw_with_reminder(f"renown {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_awaken(node: AwakenAbility) -> str:
    body = f"awaken {unparse(node.caliber)}—{unparse(node.cost)}"
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_surge(node: SurgeAbility) -> str:
    return _kw_with_reminder(f"surge {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_emerge(node: EmergeAbility) -> str:
    return _kw_with_reminder(f"emerge {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_escalate(node: EscalateAbility) -> str:
    return _kw_with_reminder(f"escalate {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_crew(node: CrewAbility) -> str:
    return _kw_with_reminder(f"crew {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_fabricate(node: FabricateAbility) -> str:
    return _kw_with_reminder(f"fabricate {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_partner(node: PartnerAbility) -> str:
    body = (
        f"partner with {unparse(node.partner_name)}"
        if node.partner_name is not None
        else "partner"
    )
    return _kw_with_reminder(body, node.reminder_text)


@unparse.register
def _unparse_embalm(node: EmbalmAbility) -> str:
    return _kw_with_reminder(f"embalm {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_eternalize(node: EternalizeAbility) -> str:
    return _kw_with_reminder(f"eternalize {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_afflict(node: AfflictAbility) -> str:
    return _kw_with_reminder(f"afflict {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_surveil(node: SurveilAbility) -> str:
    return _kw_with_reminder(f"surveil {unparse(node.caliber)}", node.reminder_text)


@unparse.register
def _unparse_jump_start(node: JumpStartAbility) -> str:
    return _kw_with_reminder(f"jump-start {unparse(node.cost)}", node.reminder_text)


@unparse.register
def _unparse_ward(node: WardAbility) -> str:
    return _kw_with_reminder(f"ward {unparse(node.cost)}", node.reminder_text)


# ---------------------------------------------------------------------------
# Fall-through / convenience
# ---------------------------------------------------------------------------


@unparse.register
def _unparse_str(node: str) -> str:
    # Lets callers pass raw strings inside compound nodes (e.g. partner name).
    return node


@unparse.register
def _unparse_int(node: int) -> str:
    return str(node)


# TODO(unparser): The following dataclasses are intentionally not registered —
# Reed left their unparseToString stubbed, so we emit nothing meaningful:
#   - GainLoseExpression (no body fields)
#   - AddRemoveExpression (no body fields)
#   - RevealExpression (no body fields)
# Calling `unparse` on instances of these will hit the NotImplementedError
# default; callers can either skip them or extend this module once the
# transformer fills in their fields.
