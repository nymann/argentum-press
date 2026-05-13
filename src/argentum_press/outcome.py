"""Per-card pipeline outcomes.

Each card processed by the pipeline produces exactly one CardOutcome. The
sealed-ish hierarchy lets callers match on the variant for reporting and
flow-control without inspecting fields. Python doesn't enforce closedness;
we lean on type checkers and the `match` statement for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Emitted:
    """The card parsed, lowered, was written, and compiled cleanly."""

    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class DeferredParseFailed:
    """mtgcompiler returned a non-ok ParseResult; we never even tried to lower."""

    name: str
    error: str  # mtgcompiler.ParseError stringified


@dataclass(frozen=True, slots=True)
class DeferredEmitterGap:
    """Parsed cleanly, but the Lowerer has no rule for one of the AST nodes."""

    name: str
    missing_node: str  # qualified class name like 'mtgcompiler.ast.SkipCombatPhases'


@dataclass(frozen=True, slots=True)
class CompileFailed:
    """Wrote a file, but gradle compileKotlin rejected it. This is where the
    LLM repair loop will eventually plug in; for now the pipeline crashes."""

    name: str
    path: Path
    stderr: str


CardOutcome = Emitted | DeferredParseFailed | DeferredEmitterGap | CompileFailed
