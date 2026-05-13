"""Classifier tests."""

from __future__ import annotations

from argentum_press import _ast as ast
from argentum_press.classify import Bucket1, Bucket2, classify
from argentum_press.lowerer import KotlinLowerer


def test_vanilla_card_is_bucket_1_with_empty_body() -> None:
    result = classify(ast.Card(), KotlinLowerer())
    assert isinstance(result, Bucket1)
    assert result.body == ""


def test_keyword_only_card_is_bucket_1() -> None:
    card = ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket1)
    assert result.body == "keywords(Keyword.FLYING)"


def test_supported_spell_is_bucket_1_with_full_body() -> None:
    card = ast.Card((ast.SpellAbility((ast.DealDamage(3, ast.AnyTarget()),)),))
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket1)
    assert "Effects.DealDamage(3" in result.body


def test_activated_ability_is_bucket_2_with_node_type() -> None:
    card = ast.Card(
        (ast.ActivatedAbility((ast.TapSelf(),), (ast.DrawCards(1),)),)
    )
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket2)
    assert "ActivatedAbility" in result.missing_node


def test_unknown_effect_is_bucket_2() -> None:
    class HypotheticalSkipCombat(ast.Effect):
        pass

    card = ast.Card(
        (
            ast.SpellAbility((HypotheticalSkipCombat(),)),
        )
    )
    result = classify(card, KotlinLowerer())
    assert isinstance(result, Bucket2)
    assert "HypotheticalSkipCombat" in result.missing_node
