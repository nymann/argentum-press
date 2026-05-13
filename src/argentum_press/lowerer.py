"""Lower a parsed AST into an argentum-engine Kotlin DSL body string.

Dispatches via functools.singledispatchmethod over the AST hierarchy
defined in _ast.py (which will become a re-export from mtgcompiler.ast
once that library is adapted).

Any AST node without a registered handler raises EmitterGap; the
pipeline turns that into CardOutcome.DeferredEmitterGap rather than
trying to guess.
"""

from __future__ import annotations

from functools import singledispatchmethod

from . import _ast


class EmitterGap(RuntimeError):
    """Raised when the lowerer encounters an AST node it doesn't yet emit.

    The node_type attribute is the qualified class name; the pipeline
    surfaces it so the deferred-emitter-gap report points directly at
    the next @register handler to write.
    """

    def __init__(self, node: object) -> None:
        cls = type(node)
        self.node_type = f"{cls.__module__}.{cls.__qualname__}"
        super().__init__(f"no lowering rule for {self.node_type}")


class KotlinLowerer:
    """AST -> Kotlin DSL body. Composes ability blocks; collapses keyword
    abilities into a single keywords(...) call as the real argentum cards do."""

    def lower_card(self, card: _ast.Card) -> str:
        if not card.abilities:
            return ""
        keywords: list[_ast.Keyword] = []
        others: list[_ast.Ability] = []
        for ability in card.abilities:
            if isinstance(ability, _ast.KeywordAbility):
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

    # ---- abilities ----

    @singledispatchmethod
    def ability(self, ability: _ast.Ability) -> str:
        raise EmitterGap(ability)

    @ability.register
    def _(self, ability: _ast.KeywordAbility) -> str:
        # Reached only when caller renders an isolated KeywordAbility;
        # lower_card collapses them before getting here.
        return f"keywords(Keyword.{ability.keyword.name})"

    @ability.register
    def _(self, ability: _ast.SpellAbility) -> str:
        return f"spell {{\n    effect = {self._chain(ability.effects)}\n}}"

    @ability.register
    def _(self, ability: _ast.TriggeredAbility) -> str:
        return (
            "triggeredAbility {\n"
            f"    trigger = Triggers.{_trigger_name(ability.condition)}\n"
            f"    effect = {self._chain(ability.effects)}\n"
            "}"
        )

    # ActivatedAbility intentionally not registered yet — argentum's DSL surface
    # for activated abilities needs a closer look before we generate them.
    # An ActivatedAbility flowing through the lowerer raises EmitterGap.

    def _chain(self, effects: tuple[_ast.Effect, ...]) -> str:
        rendered = [self.effect(e) for e in effects]
        if not rendered:
            return ""
        out = rendered[0]
        for r in rendered[1:]:
            out += f"\n        .then({r})"
        return out

    # ---- effects ----

    @singledispatchmethod
    def effect(self, effect: _ast.Effect) -> str:
        raise EmitterGap(effect)

    @effect.register
    def _(self, e: _ast.DrawCards) -> str:
        return f"Effects.DrawCards({e.amount})"

    @effect.register
    def _(self, e: _ast.GainLife) -> str:
        return f"Effects.GainLife({e.amount})"

    @effect.register
    def _(self, e: _ast.LoseLife) -> str:
        return f"Effects.LoseLife({e.amount})"

    @effect.register
    def _(self, e: _ast.DealDamage) -> str:
        return f"Effects.DealDamage({e.amount}, {self.target(e.target)})"

    @effect.register
    def _(self, e: _ast.DestroyTarget) -> str:
        return f"Effects.Destroy({self.target(e.target)})"

    @effect.register
    def _(self, e: _ast.CreateToken) -> str:
        return f'Effects.CreateToken("{e.token_name}", {e.count})'

    @effect.register
    def _(self, e: _ast.ModifyStats) -> str:
        return (
            f"Effects.ModifyStats({e.power_delta}, {e.toughness_delta}, "
            f"{self.target(e.target)})"
        )

    @effect.register
    def _(self, e: _ast.ReanimateTarget) -> str:
        return f"Effects.ReturnFromGraveyard({self.target(e.target)})"

    @effect.register
    def _(self, _e: _ast.ShuffleSelfIntoLibrary) -> str:
        return "Effects.ShuffleSelfIntoLibrary"

    # ---- targets ----

    @singledispatchmethod
    def target(self, target: _ast.Target) -> str:
        raise EmitterGap(target)

    @target.register
    def _(self, _t: _ast.AnyTarget) -> str:
        return 'target("any target", Targets.Any)'

    @target.register
    def _(self, _t: _ast.TargetCreature) -> str:
        return 'target("target creature", Targets.Creature)'

    @target.register
    def _(self, _t: _ast.TargetPlayer) -> str:
        return 'target("target player", Targets.Player)'

    @target.register
    def _(self, _t: _ast.TargetSelf) -> str:
        return "Targets.Self"


def _trigger_name(condition: _ast.TriggerCondition) -> str:
    return {
        _ast.TriggerCondition.ENTERS_BATTLEFIELD: "EntersBattlefield",
        _ast.TriggerCondition.ATTACKS: "Attacks",
        _ast.TriggerCondition.DIES: "Dies",
        _ast.TriggerCondition.BEGINNING_OF_UPKEEP: "BeginningOfUpkeep",
        _ast.TriggerCondition.END_OF_TURN: "EndOfTurn",
    }[condition]
