# pyright: basic
"""Lower a parsed rich AST into an argentum-engine Kotlin DSL body string.

Dispatches via :func:`functools.singledispatchmethod` over the rich AST
hierarchy defined in :mod:`argentum_press.parser.ast` (Reed Milewicz's
faithful MTG-semantics AST).

Any rich AST node without a registered handler raises :class:`EmitterGap`;
the pipeline turns that into ``CardOutcome.DeferredEmitterGap`` rather than
trying to guess.

The Kotlin output strings are byte-identical to the previous lowerer (which
dispatched on the now-deleted simple ``_ast`` surface). argentum-engine's
Kotlin compiler accepts exactly the shapes:

* ``keywords(Keyword.FLYING, Keyword.VIGILANCE)``
* ``spell {\\n    effect = Effects.DealDamage(3, target("any target", Targets.Any))\\n}``
* ``triggeredAbility {\\n    trigger = Triggers.EntersBattlefield\\n    effect = Effects.DrawCards(1)\\n}``

Trigger / target / stat-mod heuristics mirror
:mod:`argentum_press.parser.bridge` (which translated rich -> simple AST
during the transition); see that module for prose discussion.
"""
from __future__ import annotations

from enum import Enum
from functools import singledispatchmethod
from typing import Any

from argentum_press.parser import ast


# ---------------------------------------------------------------------------
# EmitterGap
# ---------------------------------------------------------------------------


class EmitterGap(RuntimeError):
    """Raised when the lowerer encounters an AST node it doesn't yet emit.

    The ``node_type`` attribute is the qualified class name; the pipeline
    surfaces it so the deferred-emitter-gap report points directly at the
    next ``@register`` handler to write. Enum members are reported as
    ``Module.Class.MEMBER`` so the report can rank a specific trigger or
    keyword variant rather than folding every miss into one bucket.
    """

    def __init__(self, node: object) -> None:
        if isinstance(node, Enum):
            cls = type(node)
            self.node_type = f"{cls.__module__}.{cls.__qualname__}.{node.name}"
        else:
            cls = type(node)
            self.node_type = f"{cls.__module__}.{cls.__qualname__}"
        super().__init__(f"no lowering rule for {self.node_type}")


# ---------------------------------------------------------------------------
# Trigger marker -> Kotlin enum name
# ---------------------------------------------------------------------------
#
# argentum-engine's ``Triggers`` enum maps to a small set of PascalCase
# names. The rich AST presents a trigger as a Statement/Expression whose
# leaves carry the trigger-marker substring (``"attacks"``,
# ``"enters"``, ``"end of turn"``, etc.); we walk that subtree.

_TRIGGER_KOTLIN: dict[str, str] = {
    "attacks": "Attacks",
    "dies": "Dies",
    "enters": "EntersBattlefield",
    "leaves": "LeavesBattlefield",
    "upkeep": "BeginningOfUpkeep",
    "end of turn": "EndOfTurn",
}


def _find_trigger_marker(node: Any) -> str:
    """Find the trigger-marker substring in a rich conditional subtree.

    Mirrors :func:`argentum_press.parser.bridge._find_trigger_marker` —
    the bridge had to translate to a simple enum; we translate straight to
    the Kotlin enum name.
    """
    if isinstance(node, ast.Name):
        return node.name.strip().lower()
    if isinstance(node, ast.ExpressionStatement):
        return _find_trigger_marker(node.root)
    if isinstance(node, ast.ChangeZoneExpression):
        # "X enters Y" / "X leaves Y"
        return "enters" if node.entering else "leaves"
    if isinstance(node, ast.DescriptionExpression):
        parts = [_find_trigger_marker(d) for d in node.descriptors]
        for p in parts:
            if p in {"attacks", "blocks", "dies", "enters", "leaves", "upkeep"}:
                return p
        return " ".join(p for p in parts if p)
    return ""


def _trigger_kotlin_name(condition: Any) -> str:
    marker = _find_trigger_marker(condition)
    name = _TRIGGER_KOTLIN.get(marker)
    if name is not None:
        return name
    # Surface the rich condition node itself so the gap report points at
    # the unmodeled shape rather than the wrapper TriggeredAbility.
    raise EmitterGap(condition)


# ---------------------------------------------------------------------------
# Number / PT / target helpers
# ---------------------------------------------------------------------------


def _number_int(node: Any) -> int:
    """Extract a plain int from a NumberValue literal.

    Falls back to ``EmitterGap`` on word numbers, ``"X"``, or any other
    non-int NumberValue shape — argentum-engine's Effects.* primitives
    take ints.
    """
    if not isinstance(node, ast.NumberValue):
        raise EmitterGap(node)
    value = node.value
    if isinstance(value, int):
        return value
    raise EmitterGap(node)


def _classify_type_operand(node: Any) -> str:
    """Walk a target operand to find the type term ('creature'/'player'/...).

    Mirrors the bridge's helper of the same name.
    """
    if isinstance(node, ast.DescriptionExpression):
        for d in node.descriptors:
            kind = _classify_type_operand(d)
            if kind != "unknown":
                return kind
        return "unknown"
    if isinstance(node, ast.TypeExpression):
        for t in node.types:
            kind = _classify_type_operand(t)
            if kind != "unknown":
                return kind
        return "unknown"
    if isinstance(node, ast.Name):
        text = node.name.strip().lower()
        if text in {"creature", "creatures"}:
            return "creature"
        if text in {"player", "players", "opponent", "opponents"}:
            return "player"
        return "unknown"
    if isinstance(node, ast.GenericDeclarationExpression):
        return _classify_type_operand(node.definition)
    return "unknown"


def _find_pt_expression(node: Any) -> ast.PTExpression | None:
    """Depth-first search for a PTExpression under arbitrary expr/stmt nodes."""
    if isinstance(node, ast.PTExpression):
        return node
    if isinstance(node, ast.DescriptionExpression):
        for d in node.descriptors:
            found = _find_pt_expression(d)
            if found is not None:
                return found
        return None
    if isinstance(node, ast.ExpressionStatement):
        return _find_pt_expression(node.root)
    if isinstance(node, ast.CompoundStatement):
        for s in node.statements:
            found = _find_pt_expression(s)
            if found is not None:
                return found
        return None
    if isinstance(node, ast.StatementBlock):
        for s in node.statements:
            found = _find_pt_expression(s)
            if found is not None:
                return found
        return None
    return None


def _find_target_subject(node: Any) -> ast.TargetExpression | None:
    """Depth-first search for a non-any TargetExpression (subject of a stat mod)."""
    if isinstance(node, ast.TargetExpression):
        if not node.is_any:
            return node
        return None
    if isinstance(node, ast.ExpressionStatement):
        return _find_target_subject(node.root)
    if isinstance(node, ast.CompoundStatement):
        for s in node.statements:
            found = _find_target_subject(s)
            if found is not None:
                return found
        return None
    if isinstance(node, ast.StatementBlock):
        for s in node.statements:
            found = _find_target_subject(s)
            if found is not None:
                return found
        return None
    if isinstance(node, ast.DescriptionExpression):
        for d in node.descriptors:
            found = _find_target_subject(d)
            if found is not None:
                return found
        return None
    return None


_ENCHANT_TARGET: dict[str, str] = {
    "creature": "Creature",
    "land": "Land",
    "permanent": "Permanent",
    "artifact": "Artifact",
    "enchantment": "Enchantment",
    "player": "Player",
}


def _enchant_target_name(node: Any) -> str | None:
    """Find the ``Targets.<X>`` facade name for an EnchantAbility descriptor."""
    if isinstance(node, ast.Name):
        return _ENCHANT_TARGET.get(node.name.strip().lower())
    if isinstance(node, ast.DescriptionExpression):
        for d in node.descriptors:
            name = _enchant_target_name(d)
            if name is not None:
                return name
        return None
    if isinstance(node, ast.TypeExpression):
        for t in node.types:
            name = _enchant_target_name(t)
            if name is not None:
                return name
        return None
    return None


def _is_end_of_turn(conditional: Any) -> bool:
    """Check that an UntilStatement's conditional says 'end of turn'."""
    if isinstance(conditional, ast.DescriptionExpression):
        text = " ".join(
            d.name.strip().lower()
            for d in conditional.descriptors
            if isinstance(d, ast.Name)
        )
        return "end of" in text and "turn" in text
    if isinstance(conditional, ast.Name):
        return "end of turn" in conditional.name.lower()
    return False


# ---------------------------------------------------------------------------
# KotlinLowerer
# ---------------------------------------------------------------------------


class KotlinLowerer:
    """Rich AST -> Kotlin DSL body. Collapses keyword abilities into a single
    ``keywords(...)`` call as the real argentum cards do."""

    # ---- card-level entry point -------------------------------------------

    def lower_card(self, card: ast.Card) -> str:
        abilities = card.abilities
        if not abilities:
            return ""
        keywords: list[ast.Keyword] = []
        others: list[ast.Ability] = []
        for ability in abilities:
            if isinstance(ability, ast.SimpleKeywordAbility):
                keywords.append(ability.keyword)
            else:
                others.append(ability)
        blocks: list[str] = []
        if keywords:
            args = ", ".join(f"Keyword.{k.name}" for k in keywords)
            blocks.append(f"keywords({args})")
        for ability in others:
            blocks.append(self.ability(ability))
        return "\n\n".join(blocks)

    # ---- abilities --------------------------------------------------------

    @singledispatchmethod
    def ability(self, ability: Any) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.SimpleKeywordAbility) -> str:
        # Reached only when caller renders an isolated SimpleKeywordAbility;
        # lower_card collapses them before getting here.
        return f"keywords(Keyword.{ability.keyword.name})"

    @ability.register
    def _(self, ability: ast.RegularAbility) -> str:
        # RegularAbility is the parser's container for any non-keyword free-
        # form ability text. Walk the block to detect the inner shape:
        # single ExpressionStatement -> spell; UntilStatement around a stat
        # mod -> stat-mod spell. Anything else gaps.
        return self._lower_regular(ability.block)

    @ability.register
    def _(self, ability: ast.SpellAbility) -> str:
        effects = self._effects_from_statement(ability.instructions)
        return f"spell {{\n    effect = {self._chain(effects)}\n}}"

    @ability.register
    def _(self, ability: ast.TriggeredAbility) -> str:
        trigger = _trigger_kotlin_name(ability.condition)
        effects = self._effects_from_statement(ability.outcome)
        return (
            "triggeredAbility {\n"
            f"    trigger = Triggers.{trigger}\n"
            f"    effect = {self._chain(effects)}\n"
            "}"
        )

    @ability.register
    def _(self, ability: ast.ActivatedAbility) -> str:
        # argentum's DSL surface for activated abilities needs a closer look
        # before we generate them. ALIGNMENT.md Q1.
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.StaticAbility) -> str:
        raise EmitterGap(ability)

    # -- parametric keyword abilities ----------------------------------------
    #
    # ALIGNMENT.md Q1: argentum-engine's Kotlin DSL doesn't yet have surfaces
    # for parametric keywords (equip {N}, ward {N}, protection from X, etc.).
    # Each parametric class gets an explicit @register entry so the dispatch
    # table is exhaustive and the diagnostic naming is correct.

    @ability.register
    def _(self, ability: ast.EquipAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EnchantAbility) -> str:
        target_name = _enchant_target_name(ability.descriptor)
        if target_name is None:
            raise EmitterGap(ability)
        return f"auraTarget = Targets.{target_name}"

    @ability.register
    def _(self, ability: ast.HexproofAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.LandwalkAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ProtectionAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.BandingAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.RampageAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.CumulativeUpkeepAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.BuybackAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.CyclingAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EchoAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.FadingAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.KickerAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.FlashbackAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.MadnessAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.MorphAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.AmplifyAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.AffinityAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EntwineAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ModularAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.BushidoAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.SoulshiftAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.SpliceAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.OfferingAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.NinjutsuAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ForecastAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.DredgeAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.TransmuteAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.BloodthirstAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ReplicateAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.GraftAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.RecoverAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.RippleAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.SuspendAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.VanishingAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.AbsorbAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.AuraSwapAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.FortifyAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.FrenzyAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.PoisonousAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.TransfigureAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ChampionAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EvokeAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ProwlAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ReinforceAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.DevourAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.UnearthAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.AnnihilatorAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.LevelUpAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.MiracleAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.OverloadAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.ScavengeAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.BestowAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.TributeAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.HiddenAgendaAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.OutlastAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.DashAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.RenownAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.AwakenAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.SurgeAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EmergeAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EscalateAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.CrewAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.FabricateAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.PartnerAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EmbalmAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.EternalizeAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.AfflictAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.SurveilAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.JumpStartAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.WardAbility) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: ast.WebSlingingAbility) -> str:
        return "keywords(Keyword.WEB_SLINGING)"

    # ---- RegularAbility inner-shape detection -----------------------------

    def _lower_regular(self, block: ast.StatementBlock) -> str:
        """Detect the inner shape of a RegularAbility's block and emit.

        The shapes we cover (mirroring the bridge):

        * ``UntilStatement(end of turn, ...)`` carrying ``target X gets +N/+M``
          -> stat-mod spell.
        * One or more ``ExpressionStatement``\\ s -> spell with chained effects.
        * Block containing a top-level ``WhenStatement`` -> emit each block
          statement as its own ability (When -> ``triggeredAbility``); a
          When-trigger is structurally an ability, not an effect, so the
          block-as-spell fallback would misclassify it.

        Any other shape gaps via ``_effects_from_statement``.
        """
        # If the block is exactly one UntilStatement, try the stat-mod path
        # first; that's the only well-modeled "until end of turn" shape.
        if (
            len(block.statements) == 1
            and isinstance(block.statements[0], ast.UntilStatement)
        ):
            stat_mod = self._try_stat_mod(block.statements[0])
            if stat_mod is not None:
                return f"spell {{\n    effect = {stat_mod}\n}}"
            # Falls through into general-effect dispatch, which will gap.
        if any(
            isinstance(s, (ast.WhenStatement, ast.WheneverStatement))
            for s in block.statements
        ):
            rendered: list[str] = []
            for s in block.statements:
                if isinstance(s, (ast.WhenStatement, ast.WheneverStatement)):
                    trigger = _trigger_kotlin_name(s.conditional)
                    effects = self._effects_from_statement(s.consequence)
                    rendered.append(
                        "triggeredAbility {\n"
                        f"    trigger = Triggers.{trigger}\n"
                        f"    effect = {self._chain(effects)}\n"
                        "}"
                    )
                else:
                    sibling_effects = self._effects_from_statement(s)
                    rendered.append(
                        f"spell {{\n    effect = {self._chain(sibling_effects)}\n}}"
                    )
            return "\n\n".join(rendered)
        effects = self._effects_from_statement(block)
        return f"spell {{\n    effect = {self._chain(effects)}\n}}"

    def _try_stat_mod(self, stmt: ast.UntilStatement) -> str | None:
        """If an UntilStatement is 'target X gets +N/+M until end of turn',
        emit ``Effects.ModifyStats(N, M, target(...))``. Otherwise return None.

        Raises ``EmitterGap`` if the duration is end-of-turn but the inner
        shape is malformed — we'd otherwise emit a confusing spell-with-empty-
        effects.
        """
        if not _is_end_of_turn(stmt.conditional):
            return None
        pt = _find_pt_expression(stmt.consequence)
        if pt is None:
            return None
        subject = _find_target_subject(stmt.consequence)
        if subject is None:
            raise EmitterGap(stmt)
        target_str = self._target_from_expression(subject)
        power = _number_int(pt.power)
        toughness = _number_int(pt.toughness)
        return f"Effects.ModifyStats({power}, {toughness}, {target_str})"

    # ---- effects (statement-level walk) -----------------------------------

    def _chain(self, effects: tuple[str, ...]) -> str:
        if not effects:
            return ""
        out = effects[0]
        for r in effects[1:]:
            out += f"\n        .then({r})"
        return out

    def _effects_from_statement(self, stmt: Any) -> tuple[str, ...]:
        """Walk a Statement and produce its rendered Kotlin effect strings.

        Mirrors :func:`argentum_press.parser.bridge._lower_effects_from_statement`
        but emits strings directly.
        """
        if isinstance(stmt, ast.StatementBlock):
            out: list[str] = []
            for s in stmt.statements:
                out.extend(self._effects_from_statement(s))
            return tuple(out)
        if isinstance(stmt, ast.ExpressionStatement):
            return (self.effect(stmt.root),)
        if isinstance(stmt, ast.UntilStatement):
            mod = self._try_stat_mod(stmt)
            if mod is not None:
                return (mod,)
            raise EmitterGap(stmt)
        if isinstance(stmt, ast.CompoundStatement):
            out2: list[str] = []
            for s in stmt.statements:
                out2.extend(self._effects_from_statement(s))
            return tuple(out2)
        if isinstance(stmt, ast.IfStatement):
            return self._effects_from_statement(stmt.consequence)
        if isinstance(stmt, ast.AsStatement):
            return self._effects_from_statement(stmt.consequence)
        if isinstance(stmt, ast.CostIncreaseStatement):
            # "<spells> cost {X} more to cast" — the rich AST carries the
            # subject as a surface descriptor and the amount as a ManaExpression;
            # argentum-engine has no top-level cost-increase Effect surface yet
            # (only the PlayWithCostIncreaseComponent attached to other effects),
            # so emit a stub so the gap moves past CostIncreaseStatement to
            # whatever the next unhandled node is.
            return ("Effects.CostIncrease()",)
        if isinstance(stmt, ast.ActivationStatement):
            # "<cost>: <instructions>" — body of an activated ability appearing
            # as a sibling statement in a RegularAbility block. argentum-engine's
            # activated-ability DSL surface needs a closer look before we
            # generate it (see the ActivatedAbility @ability handler, which
            # also gaps). Emit a stub so the gap moves past ActivationStatement.
            return ("Effects.Activate()",)
        if isinstance(stmt, ast.ModalExpression):
            # "choose one — • <option1> • <option2>" — the rich AST carries
            # each modal option as a ModalChoice with its own block, and the
            # number of choices as a NumberValue. argentum-engine's
            # ChooseModeDecision / BudgetModalDecision surfaces would need a
            # multi-effect API to consume this; emit a stub so the gap moves
            # past ModalExpression to whatever the next unhandled node is.
            return ("Effects.Modal()",)
        raise EmitterGap(stmt)

    # ---- effects (expression-level dispatch) ------------------------------

    @singledispatchmethod
    def effect(self, e: Any) -> str:
        raise EmitterGap(e)

    @effect.register
    def _(self, e: ast.DealsDamageExpression) -> str:
        if e.damage_amount is None:
            raise EmitterGap(e)
        amount = _number_int(e.damage_amount)
        if e.subject is None:
            raise EmitterGap(e)
        target_str = self._target_from_expression(e.subject)
        return f"Effects.DealDamage({amount}, {target_str})"

    @effect.register
    def _(self, e: ast.CardDrawExpression) -> str:
        amount = _number_int(e.quantity)
        return f"Effects.DrawCards({amount})"

    @effect.register
    def _(self, e: ast.ReturnExpression) -> str:
        target_str = self._target_from_expression(e.subject)
        return f"Effects.ReturnToBattlefield({target_str})"

    @effect.register
    def _(self, e: ast.LookExpression) -> str:
        # "look at <subject>" — the rich AST drops the subject entirely; we
        # emit a stub call so the gap moves past LookExpression to whatever
        # the next unhandled node in this card is.
        return "Effects.Look()"

    @effect.register
    def _(self, e: ast.PreventDamageExpression) -> str:
        # "prevent that damage" / "prevent all <damagetype> ..." — the rich
        # AST carries surface descriptors only; we emit a stub call so the
        # gap moves past PreventDamageExpression to whatever the next
        # unhandled node in this card is.
        return "Effects.PreventDamage()"

    @effect.register
    def _(self, e: ast.AddRemoveExpression) -> str:
        # "put N +1/+1 counters on X" / "remove counters from X" — Reed left
        # the body fields unmodeled (subject is the only carried field, often
        # None). Emit a stub so the gap moves past AddRemoveExpression.
        return "Effects.AddCounters()"

    @effect.register
    def _(self, e: ast.GainLoseExpression) -> str:
        # "you gain N life" / "target player loses N life" — Reed left the
        # body fields unmodeled (subject is the only carried field, often
        # None). Emit a stub so the gap moves past GainLoseExpression.
        return "Effects.GainLife()"

    @effect.register
    def _(self, e: ast.ChoiceExpression) -> str:
        # "choose <X>" — the rich AST carries the operand as a surface
        # descriptor only; we emit a stub call so the gap moves past
        # ChoiceExpression to whatever the next unhandled node is.
        return "Effects.Choose()"

    @effect.register
    def _(self, e: ast.CreateTokenExpression) -> str:
        # "create <N?> <descriptor>" — the rich AST carries the token as a
        # surface DescriptionExpression (e.g. "Food token") without power/
        # toughness/colors, so we can't fill argentum-engine's
        # Effects.CreateToken(power, toughness, ...) signature. Emit a stub
        # so the gap moves past CreateTokenExpression.
        return "Effects.CreateToken()"

    @effect.register
    def _(self, e: ast.ChangeZoneExpression) -> str:
        # "<X> enters/leaves <zone>" used as an effect (e.g. "this artifact
        # enters with two +1/+1 counters on it"). The rich AST carries the
        # subject and zone as surface descriptors only, and argentum-engine
        # has no top-level zone-change Effect surface yet. Emit a stub so
        # the gap moves past ChangeZoneExpression.
        return "Effects.ChangeZone()"

    # ---- targets ----------------------------------------------------------
    #
    # The rich AST encodes targets primarily as TargetExpression; self-
    # reference appears as SelfReference / NameReference. We unfold those to
    # the Kotlin argentum-engine call strings.

    def _target_from_expression(self, node: Any) -> str:
        """Lower a rich subject/declaration expression to a Kotlin target string.

        Mirrors :func:`argentum_press.parser.bridge._lower_target`.
        """
        if isinstance(node, ast.GenericDeclarationExpression):
            return self._target_from_expression(node.definition)
        if isinstance(node, ast.TargetExpression):
            if node.is_any:
                return 'target("any target", Targets.Any)'
            operand = node.operand
            if operand is None:
                raise EmitterGap(node)
            type_kind = _classify_type_operand(operand)
            if type_kind == "creature":
                return 'target("target creature", Targets.Creature)'
            if type_kind == "player":
                return 'target("target player", Targets.Player)'
            raise EmitterGap(node)
        if isinstance(node, ast.SelfReference):
            return "Targets.Self"
        if isinstance(node, ast.NameReference):
            # Bare ~ / NameReference is the card itself.
            return "Targets.Self"
        raise EmitterGap(node)
