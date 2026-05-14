"""Classifier tests.

After the rich-AST absorption, tests construct rich-AST inputs directly.
The classify() outputs (Bucket1 body / Bucket2 missing_node) are
byte-identical to the previous (simple-AST) implementation.
"""

from __future__ import annotations

from argentum_press.classify import Bucket1, Bucket2, classify
from argentum_press.lowerer import KotlinLowerer
from argentum_press.parser import ast


def _num(n: int) -> ast.NumberValue:
    return ast.NumberValue(value=n)


def _any_target() -> ast.TargetExpression:
    return ast.TargetExpression(operand=None, is_any=True)


def _deal_damage(amount: int, subject: ast.Expression) -> ast.DealsDamageExpression:
    return ast.DealsDamageExpression(
        origin=ast.NameReference(),
        damage_type=ast.DamageType(value=ast.DamageTypeEnum.REGULAR),
        damage_amount=_num(amount),
        subject=subject,
    )


def _spell(*roots: ast.Expression) -> ast.RegularAbility:
    statements = tuple(ast.ExpressionStatement(root=r) for r in roots)
    return ast.RegularAbility(block=ast.StatementBlock(statements=statements))


def test_vanilla_card_is_bucket_1_with_empty_body() -> None:
    result = classify(ast.Card(), KotlinLowerer())
    assert isinstance(result, Bucket1)
    assert result.body == ""


def test_keyword_only_card_is_bucket_1() -> None:
    card = ast.Card(
        abilities=(ast.SimpleKeywordAbility(keyword=ast.Keyword.FLYING),),
    )
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket1)
    assert result.body == "keywords(Keyword.FLYING)"


def test_supported_spell_is_bucket_1_with_full_body() -> None:
    card = ast.Card(abilities=(_spell(_deal_damage(3, _any_target())),))
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket1)
    assert "Effects.DealDamage(3" in result.body


def test_activated_ability_is_bucket_2_with_node_type() -> None:
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
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket2)
    assert "ActivatedAbility" in result.missing_node


def test_unknown_effect_is_bucket_2() -> None:
    # A rich expression with no registered effect handler (Sacrifice has
    # no @effect.register entry today) drives a Bucket2 result.
    card = ast.Card(
        abilities=(_spell(ast.SacrificeExpression(subject=ast.NameReference())),),
    )
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket2)
    assert "SacrificeExpression" in result.missing_node
