"""Smoke-level coverage of the CardOutcome variants.

The point is less to test dataclass machinery (Python handles that) and more
to pin the variant set with a `match` statement: if a new variant lands without
a case here, mypy / a future exhaustiveness check will surface it.
"""

from __future__ import annotations

from pathlib import Path

from argentum_press.outcome import (
    CardOutcome,
    CompileFailed,
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)


def classify(outcome: CardOutcome) -> str:
    match outcome:
        case Emitted():
            return "emitted"
        case DeferredParseFailed():
            return "deferred-parse"
        case DeferredEmitterGap():
            return "deferred-gap"
        case CompileFailed():
            return "compile-failed"


def test_emitted_classifies() -> None:
    assert classify(Emitted("X", Path("/tmp/X.kt"))) == "emitted"


def test_deferred_parse_failed_classifies() -> None:
    assert classify(DeferredParseFailed("Y", "incomplete parse at offset 12")) == "deferred-parse"


def test_deferred_emitter_gap_classifies() -> None:
    assert (
        classify(DeferredEmitterGap("Z", "mtgcompiler.ast.SkipCombatPhases")) == "deferred-gap"
    )


def test_compile_failed_classifies() -> None:
    assert (
        classify(CompileFailed("Q", Path("/tmp/Q.kt"), "kotlinc: unresolved")) == "compile-failed"
    )
