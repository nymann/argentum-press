# pyright: basic
"""Tests for the deterministic L3 (TF-IDF + heuristics).

The integration path (lower playbook calling deterministic_l3) is
exercised by the existing test_playbook_lower happy-path tests. These
tests pin down the helpers directly so a regression in the suffix-
strip / docstring extraction / TF-IDF ranking surfaces here first.
"""
from __future__ import annotations

from pathlib import Path

from argentum_press.playbook import l3_index
from argentum_press.playbook.context import (
    AstClassInfo,
    LowererExemplars,
    RegisterHandler,
)


# ---------------------------------------------------------------------------
# mtg_term suffix-strip
# ---------------------------------------------------------------------------


def test_derive_mtg_term_strips_known_suffixes():
    assert l3_index._derive_mtg_term("DiesExpression") == "dies"
    assert l3_index._derive_mtg_term("AwakenAbility") == "awaken"
    assert l3_index._derive_mtg_term("WhenStatement") == "when"
    assert l3_index._derive_mtg_term("NameReference") == "name"
    assert l3_index._derive_mtg_term("ZoneDeclaration") == "zone"


def test_derive_mtg_term_lowercases_when_no_suffix_matches():
    assert l3_index._derive_mtg_term("Card") == "card"
    assert l3_index._derive_mtg_term("Foo") == "foo"


# ---------------------------------------------------------------------------
# summary derivation
# ---------------------------------------------------------------------------


def _ast_info(
    classname: str,
    *,
    source: str,
    fields: tuple[tuple[str, str], ...] = (),
    parent_module: str = "statements",
) -> AstClassInfo:
    return AstClassInfo(
        path=Path("/fake.py"),
        classname=classname,
        source=source,
        fields=fields,
        parent_module=parent_module,
    )


def test_derive_summary_prefers_docstring_first_line():
    src = (
        '@dataclass(frozen=True, slots=True)\n'
        'class DiesExpression(Expression):\n'
        '    """Trigger condition for ``a creature dies`` clauses.\n\n'
        '    The optional subject distinguishes self-dies from any-creature-dies.\n'
        '    """\n'
        '    subject: Expression | None = None\n'
    )
    info = _ast_info("DiesExpression", source=src)
    assert (
        l3_index._derive_summary(info)
        == "Trigger condition for ``a creature dies`` clauses."
    )


def test_derive_summary_falls_back_to_shape_when_no_docstring():
    src = (
        '@dataclass(frozen=True, slots=True)\n'
        'class FooExpression(Expression):\n'
        '    bar: str\n'
    )
    info = _ast_info(
        "FooExpression",
        source=src,
        fields=(("bar", "str"),),
        parent_module="expressions",
    )
    out = l3_index._derive_summary(info)
    assert "FooExpression" in out
    assert "expressions.py" in out
    assert "bar: str" in out


def test_derive_summary_handles_empty_docstring():
    src = (
        '@dataclass(frozen=True, slots=True)\n'
        'class EmptyDoc(Expression):\n'
        '    """"""\n'
        '    pass\n'
    )
    info = _ast_info("EmptyDoc", source=src)
    out = l3_index._derive_summary(info)
    assert "EmptyDoc" in out


# ---------------------------------------------------------------------------
# TF-IDF similar_handlers
# ---------------------------------------------------------------------------


def _handler(ast_class: str, body: str, dispatcher: str = "ability") -> RegisterHandler:
    return RegisterHandler(
        dispatcher=dispatcher,
        ast_class=ast_class,
        body=body,
        line=0,
    )


def test_top_k_returns_empty_when_no_handlers():
    exemplars = LowererExemplars(register_handlers=(), isinstance_branches=())
    out = l3_index._top_k_similar_handlers(
        query_source="anything", exemplars=exemplars, k=5,
    )
    assert out == []


def test_top_k_ranks_handlers_by_token_overlap():
    exemplars = LowererExemplars(
        register_handlers=(
            _handler(
                "ast.AwakenAbility",
                "return f\"awaken {{ count = {ability.count} }}\"",
            ),
            _handler(
                "ast.EnchantAbility",
                "return f\"enchant {{ target = '{ability.target}' }}\"",
            ),
            _handler(
                "ast.SpellAbility",
                "return f\"spell {{ effect = ... }}\"",
            ),
        ),
        isinstance_branches=(),
    )
    # Query mentions "awaken" — TF-IDF should rank AwakenAbility first.
    query = "class FooAwakenAbility:\n    count: int  # awaken count"
    out = l3_index._top_k_similar_handlers(
        query_source=query, exemplars=exemplars, k=3,
    )
    assert out[0] == "AwakenAbility", out


def test_top_k_strips_ast_prefix():
    exemplars = LowererExemplars(
        register_handlers=(_handler("ast.AwakenAbility", "return ''"),),
        isinstance_branches=(),
    )
    out = l3_index._top_k_similar_handlers(
        query_source="awaken", exemplars=exemplars, k=5,
    )
    assert out == ["AwakenAbility"]


def test_top_k_dedupes_same_ast_class():
    # If the same ast_class appears twice in handlers (e.g. dispatch on
    # multiple supertypes), it should only appear once in the result.
    exemplars = LowererExemplars(
        register_handlers=(
            _handler("ast.AwakenAbility", "body1", dispatcher="ability"),
            _handler("ast.AwakenAbility", "body2", dispatcher="effect"),
        ),
        isinstance_branches=(),
    )
    out = l3_index._top_k_similar_handlers(
        query_source="awaken", exemplars=exemplars, k=5,
    )
    assert out == ["AwakenAbility"]


# ---------------------------------------------------------------------------
# deterministic_l3 (full surface)
# ---------------------------------------------------------------------------


def test_deterministic_l3_returns_schema_shaped_dict():
    src = (
        'class DiesExpression(Expression):\n'
        '    """Trigger for creature death events."""\n'
        '    subject: Expression | None = None\n'
    )
    info = _ast_info(
        "DiesExpression", source=src,
        fields=(("subject", "Expression | None"),),
        parent_module="expressions",
    )
    exemplars = LowererExemplars(
        register_handlers=(
            _handler("ast.SacrificeExpression", "return 'sacrifice ...'"),
            _handler("ast.ExileExpression", "return 'exile ...'"),
            _handler("ast.DestroyExpression", "return 'destroy ...'"),
        ),
        isinstance_branches=(),
    )
    out = l3_index.deterministic_l3(info, exemplars)
    assert set(out.keys()) == {"summary", "mtg_term", "similar_handlers"}
    assert out["mtg_term"] == "dies"
    assert "creature death" in out["summary"].lower()
    # All neighbours come from the supplied corpus, prefix-stripped.
    assert all(name in {"SacrificeExpression", "ExileExpression", "DestroyExpression"}
               for name in out["similar_handlers"])
