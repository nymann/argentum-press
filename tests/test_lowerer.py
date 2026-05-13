"""Lowerer tests — exercises each handler and confirms unregistered AST nodes
raise EmitterGap with the qualified node type so the pipeline's deferred-gap
report points somewhere useful.
"""

from __future__ import annotations

import pytest

from argentum_press import _ast as ast
from argentum_press.lowerer import EmitterGap, KotlinLowerer


def test_vanilla_card_emits_empty_body() -> None:
    assert KotlinLowerer().lower_card(ast.Card()) == ""


def test_single_keyword_emits_keywords_variadic() -> None:
    card = ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
    assert KotlinLowerer().lower_card(card) == "keywords(Keyword.FLYING)"


def test_multiple_keywords_collapse_into_one_keywords_call() -> None:
    card = ast.Card(
        (
            ast.KeywordAbility(ast.Keyword.FLYING),
            ast.KeywordAbility(ast.Keyword.TRAMPLE),
            ast.KeywordAbility(ast.Keyword.LIFELINK),
        )
    )
    assert (
        KotlinLowerer().lower_card(card)
        == "keywords(Keyword.FLYING, Keyword.TRAMPLE, Keyword.LIFELINK)"
    )


def test_lightning_bolt_emits_spell_with_inline_target() -> None:
    card = ast.Card((ast.SpellAbility((ast.DealDamage(3, ast.AnyTarget()),)),))
    out = KotlinLowerer().lower_card(card)
    assert out.startswith("spell {")
    assert 'effect = Effects.DealDamage(3, target("any target", Targets.Any))' in out


def test_chained_effects_use_then_calls() -> None:
    card = ast.Card(
        (
            ast.SpellAbility(
                (
                    ast.DealDamage(2, ast.TargetCreature()),
                    ast.GainLife(2),
                )
            ),
        )
    )
    out = KotlinLowerer().lower_card(card)
    assert 'Effects.DealDamage(2, target("target creature", Targets.Creature))' in out
    assert ".then(Effects.GainLife(2))" in out


def test_etb_draw_emits_triggered_ability() -> None:
    card = ast.Card(
        (
            ast.TriggeredAbility(
                ast.TriggerCondition.ENTERS_BATTLEFIELD,
                (ast.DrawCards(1),),
            ),
        )
    )
    out = KotlinLowerer().lower_card(card)
    assert "triggeredAbility {" in out
    assert "trigger = Triggers.EntersBattlefield" in out
    assert "effect = Effects.DrawCards(1)" in out


def test_keyword_and_triggered_separated_by_blank_line() -> None:
    card = ast.Card(
        (
            ast.KeywordAbility(ast.Keyword.FLYING),
            ast.TriggeredAbility(
                ast.TriggerCondition.ENTERS_BATTLEFIELD, (ast.DrawCards(1),)
            ),
        )
    )
    out = KotlinLowerer().lower_card(card)
    assert "keywords(Keyword.FLYING)\n\ntriggeredAbility {" in out


def test_modify_stats_renders_target() -> None:
    out = KotlinLowerer().effect(ast.ModifyStats(1, 1, ast.TargetCreature()))
    assert out == 'Effects.ModifyStats(1, 1, target("target creature", Targets.Creature))'


def test_reanimate_renders_target() -> None:
    out = KotlinLowerer().effect(ast.ReanimateTarget(ast.TargetCreature()))
    assert (
        out == 'Effects.ReturnFromGraveyard(target("target creature", Targets.Creature))'
    )


def test_shuffle_self_into_library() -> None:
    out = KotlinLowerer().effect(ast.ShuffleSelfIntoLibrary())
    assert out == "Effects.ShuffleSelfIntoLibrary"


def test_create_token_emits_name_and_count() -> None:
    out = KotlinLowerer().effect(ast.CreateToken("Soldier", 2))
    assert out == 'Effects.CreateToken("Soldier", 2)'


def test_activated_ability_is_not_yet_supported() -> None:
    card = ast.Card(
        (ast.ActivatedAbility((ast.TapSelf(),), (ast.DrawCards(1),)),)
    )
    with pytest.raises(EmitterGap) as info:
        KotlinLowerer().lower_card(card)
    assert "ActivatedAbility" in info.value.node_type


def test_emitter_gap_carries_qualified_node_type() -> None:
    # An effect with no registered handler.
    class HypotheticalSkipCombat(ast.Effect):
        pass

    with pytest.raises(EmitterGap) as info:
        KotlinLowerer().effect(HypotheticalSkipCombat())
    assert "HypotheticalSkipCombat" in info.value.node_type
