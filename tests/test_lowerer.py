"""Lowerer tests — exercises each handler and confirms unregistered AST nodes
raise EmitterGap with the qualified node type so the pipeline's deferred-gap
report points somewhere useful.

After the rich-AST absorption, the lowerer dispatches on the rich AST shapes
defined in :mod:`argentum_press.parser.ast`. Each test constructs the rich-
AST input that the parser would produce for the corresponding card text;
the Kotlin output strings are byte-identical to the previous (simple-AST)
implementation.
"""

from __future__ import annotations

import pytest

from argentum_press.lowerer import EmitterGap, KotlinLowerer
from argentum_press.parser import ast


# ---- helpers ----------------------------------------------------------------


def _num(n: int) -> ast.NumberValue:
    return ast.NumberValue(value=n)


def _any_target() -> ast.TargetExpression:
    return ast.TargetExpression(operand=None, is_any=True)


def _target_creature() -> ast.TargetExpression:
    return ast.TargetExpression(
        operand=ast.DescriptionExpression(
            descriptors=(ast.TypeExpression(types=(ast.Name(name="creature"),)),),
        ),
        is_any=False,
    )


def _deal_damage(amount: int, subject: ast.Expression) -> ast.DealsDamageExpression:
    return ast.DealsDamageExpression(
        origin=ast.NameReference(),
        damage_type=ast.DamageType(value=ast.DamageTypeEnum.REGULAR),
        damage_amount=_num(amount),
        subject=subject,
    )


def _spell(*roots: ast.Expression) -> ast.RegularAbility:
    """Build a RegularAbility whose block is a sequence of ExpressionStatements.

    The lowerer treats this as a non-keyword "spell" and emits
    ``spell { effect = ... }`` (chained with ``.then(...)`` for multiple).
    """
    statements = tuple(ast.ExpressionStatement(root=r) for r in roots)
    return ast.RegularAbility(block=ast.StatementBlock(statements=statements))


def _triggered_etb(*effect_roots: ast.Expression) -> ast.TriggeredAbility:
    """Build a TriggeredAbility whose condition resolves to ENTERS_BATTLEFIELD.

    The lowerer detects the ``enters`` trigger marker via
    :func:`_find_trigger_marker`, which recognises a ``ChangeZoneExpression``
    with ``entering=True``.
    """
    condition = ast.ExpressionStatement(
        root=ast.ChangeZoneExpression(
            subject=ast.NameReference(),
            zone=ast.Name(name="the battlefield"),
            entering=True,
        )
    )
    outcome = ast.StatementBlock(
        statements=tuple(ast.ExpressionStatement(root=r) for r in effect_roots)
    )
    return ast.TriggeredAbility(condition=condition, outcome=outcome)


def _stat_mod_until_eot(power: int, toughness: int) -> ast.RegularAbility:
    """Build ``Target creature gets +N/+M until end of turn`` as a RegularAbility."""
    pt = ast.PTExpression(power=_num(power), toughness=_num(toughness))
    consequence = ast.CompoundStatement(
        statements=(
            ast.ExpressionStatement(root=_target_creature()),
            ast.ExpressionStatement(
                root=ast.DescriptionExpression(descriptors=(pt,)),
            ),
        ),
        terminator=ast.CompoundTerminator.AND,
    )
    until = ast.UntilStatement(
        conditional=ast.DescriptionExpression(
            descriptors=(ast.Name(name="end of"), ast.Name(name="turn")),
        ),
        consequence=consequence,
        inverted=True,
    )
    return ast.RegularAbility(block=ast.StatementBlock(statements=(until,)))


# ---- tests ------------------------------------------------------------------


def test_vanilla_card_emits_empty_body() -> None:
    assert KotlinLowerer().lower_card(ast.Card()) == ""


def test_single_keyword_emits_keywords_variadic() -> None:
    card = ast.Card(
        abilities=(ast.SimpleKeywordAbility(keyword=ast.Keyword.FLYING),),
    )
    assert KotlinLowerer().lower_card(card) == "keywords(Keyword.FLYING)"


def test_multiple_keywords_collapse_into_one_keywords_call() -> None:
    card = ast.Card(
        abilities=(
            ast.SimpleKeywordAbility(keyword=ast.Keyword.FLYING),
            ast.SimpleKeywordAbility(keyword=ast.Keyword.TRAMPLE),
            ast.SimpleKeywordAbility(keyword=ast.Keyword.LIFELINK),
        )
    )
    assert (
        KotlinLowerer().lower_card(card)
        == "keywords(Keyword.FLYING, Keyword.TRAMPLE, Keyword.LIFELINK)"
    )


def test_lightning_bolt_emits_spell_with_inline_target() -> None:
    card = ast.Card(abilities=(_spell(_deal_damage(3, _any_target())),))
    out = KotlinLowerer().lower_card(card)
    assert out.startswith("spell {")
    assert 'effect = Effects.DealDamage(3, target("any target", Targets.Any))' in out


def test_chained_effects_use_then_calls() -> None:
    # Two ExpressionStatements in one RegularAbility -> chained .then(...).
    card = ast.Card(
        abilities=(
            _spell(
                _deal_damage(2, _target_creature()),
                # Draw is the only other supported effect-expression today.
                ast.CardDrawExpression(quantity=_num(1)),
            ),
        )
    )
    out = KotlinLowerer().lower_card(card)
    assert 'Effects.DealDamage(2, target("target creature", Targets.Creature))' in out
    assert ".then(Effects.DrawCards(1))" in out


def test_etb_draw_emits_triggered_ability() -> None:
    card = ast.Card(
        abilities=(_triggered_etb(ast.CardDrawExpression(quantity=_num(1))),),
    )
    out = KotlinLowerer().lower_card(card)
    assert "triggeredAbility {" in out
    assert "trigger = Triggers.EntersBattlefield" in out
    assert "effect = Effects.DrawCards(1)" in out


def test_keyword_and_triggered_separated_by_blank_line() -> None:
    card = ast.Card(
        abilities=(
            ast.SimpleKeywordAbility(keyword=ast.Keyword.FLYING),
            _triggered_etb(ast.CardDrawExpression(quantity=_num(1))),
        )
    )
    out = KotlinLowerer().lower_card(card)
    assert "keywords(Keyword.FLYING)\n\ntriggeredAbility {" in out


def test_modify_stats_renders_target() -> None:
    # ``Target creature gets +1/+1 until end of turn`` -> ModifyStats.
    card = ast.Card(abilities=(_stat_mod_until_eot(1, 1),))
    out = KotlinLowerer().lower_card(card)
    assert (
        'Effects.ModifyStats(1, 1, target("target creature", Targets.Creature))' in out
    )


def test_activated_ability_is_not_yet_supported() -> None:
    # An ActivatedAbility with arbitrary cost/instructions reaches the
    # ActivatedAbility @register entry, which raises EmitterGap.
    card = ast.Card(
        abilities=(
            ast.ActivatedAbility(
                cost=ast.Name(name="{T}"),
                instructions=ast.ExpressionStatement(
                    root=ast.CardDrawExpression(quantity=_num(1)),
                ),
            ),
        )
    )
    with pytest.raises(EmitterGap) as info:
        KotlinLowerer().lower_card(card)
    assert "ActivatedAbility" in info.value.node_type


def test_emitter_gap_carries_qualified_node_type() -> None:
    # A rich expression with no registered effect handler.
    expr = ast.SacrificeExpression(subject=ast.NameReference())
    with pytest.raises(EmitterGap) as info:
        KotlinLowerer().effect(expr)
    assert "SacrificeExpression" in info.value.node_type


def test_emitter_gap_uses_qualified_class_name() -> None:
    # The node_type carries module + qualname so the deferred-gap report
    # can rank by exact rich-AST class.
    card = ast.Card(
        abilities=(ast.EquipAbility(cost=ast.Name(name="{2}")),),
    )
    with pytest.raises(EmitterGap) as info:
        KotlinLowerer().lower_card(card)
    assert "EquipAbility" in info.value.node_type
