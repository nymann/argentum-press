"""Classify a parsed card AST as either bucket-1 (we can emit it now) or
bucket-2 (the lowerer hits an EmitterGap, meaning the card needs at least
one argentum-engine primitive we don't yet lower into).

The classifier and the emitter are the same operation: dry-run the lowerer.
We return the rendered body from a successful classification rather than
discarding it, so the pipeline doesn't have to call the lowerer a second
time during the emit phase.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import _ast
from .lowerer import EmitterGap, KotlinLowerer


@dataclass(frozen=True, slots=True)
class Bucket1:
    """All AST nodes have a registered handler; the body is the lowered DSL."""

    body: str


@dataclass(frozen=True, slots=True)
class Bucket2:
    """At least one AST node has no handler. The qualified class name of the
    first unsupported node is recorded so the report can rank the gaps."""

    missing_node: str


Classification = Bucket1 | Bucket2


def classify(ast: _ast.Card, lowerer: KotlinLowerer) -> Classification:
    try:
        body = lowerer.lower_card(ast)
    except EmitterGap as gap:
        return Bucket2(missing_node=gap.node_type)
    return Bucket1(body=body)
